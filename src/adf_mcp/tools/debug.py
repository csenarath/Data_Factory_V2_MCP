"""Toolset `debug`: run / rerun pipelines, wait, diagnose failures, debug-run unpublished Git versions,
and mapping data flow debug sessions.

ADF has no public API for ADF Studio's Debug button, and runs always execute the *published* definition.
adf_pipeline_debug_run bridges that: it publishes the Git (or supplied) version under a temporary
`zz_mcp_debug_*` name, runs it, and deletes it afterwards.
"""

from __future__ import annotations

import json
import time

from azure.core.exceptions import ResourceNotFoundError
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..checks import fmt
from ..compact import activity_run_view, pipeline_run_view, truncate
from ..diagnose import classify
from ..errors import AdfMcpError, handle_errors
from ..guards import audit, authoring_mode, check_write
from ..jobs import bounded, run_job
from ..probes import PROBE_DATASET, build_probe
from ..resources import KINDS, find_activity, references, same_definition
from ..stores import GitStore, LiveStore, Source, git_repo, resolve_store
from ..tempdeploy import DEBUG_PREFIX, TempPlan, debug_copies, execute, original_name
from ._common import TERMINAL, ctx, failed_activities, get_run, query_activity_runs, wait_for_run

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
RUN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


def _outcome(client, target, run: dict) -> dict:
    view = pipeline_run_view(run, message_chars=1000)
    if run.get("status") == "Failed":
        view["failed_activities"] = failed_activities(client, target, run)
    elif run.get("status") not in TERMINAL:
        view["still_running"] = True
        view["next"] = f"Call adf_run_wait run_id={run.get('runId')} to keep waiting."
    return view


def _start(client, target, pipeline: str, parameters: dict | None, **rerun) -> str:
    response = client.pipelines.create_run(target.resource_group, target.factory_name, pipeline, parameters or None, **rerun)
    return response.as_dict()["runId"]


def _leaf_failures(client, target, run: dict, chain: list[str], depth: int = 0) -> list[tuple[dict, dict, list[str]]]:
    """Failed activities at the bottom of Execute Pipeline chains: (run, activity_run, pipeline chain)."""
    chain = chain + [run.get("pipelineName")]
    activities, _ = query_activity_runs(client, target, run, limit=500)
    leaves = []
    for activity in activities:
        if activity.get("status") != "Failed":
            continue
        output = activity.get("output") if isinstance(activity.get("output"), dict) else {}
        child_id = output.get("pipelineRunId")
        if activity.get("activityType") == "ExecutePipeline" and child_id and depth < 8:
            leaves.extend(_leaf_failures(client, target, get_run(client, target, child_id), chain, depth + 1))
        else:
            leaves.append((run, activity, chain))
    return leaves


def _involved(store, cache: dict, pipeline: str, activity_name: str) -> dict:
    """Datasets, linked services and IRs an activity uses, from `store` (live, or Git for debug-run copies)."""
    key = ("pipeline", pipeline)
    if key not in cache:
        cache[key] = store.get(KINDS["pipeline"], pipeline) or {}
    activity = find_activity(cache[key], activity_name) or {}
    refs = set(references(activity))
    for kind, name in list(refs):
        if kind == "dataset":
            cache.setdefault(("dataset", name), store.get(KINDS["dataset"], name) or {})
            refs |= references(cache[("dataset", name)])
    for kind, name in list(refs):
        if kind == "linkedService":
            cache.setdefault(("linkedService", name), store.get(KINDS["linkedService"], name) or {})
            ls = cache[("linkedService", name)]
            refs |= references(ls)
            refs.add(("integrationRuntime", (ls.get("connectVia") or {}).get("referenceName") or "AutoResolveIntegrationRuntime"))
    return {"resources": sorted(fmt(r) for r in refs if r[0] != "pipeline"), "policy": activity.get("policy")}


def _debug_package(store, dataflow: str, row_limit: int) -> tuple[dict, list[str]]:
    """A DataFlowDebugPackage: the data flow plus every dataset and linked service it needs, from `store`."""
    flow = store.get(KINDS["dataflow"], dataflow)
    if flow is None:
        raise AdfMcpError("not_found", f"dataflow '{dataflow}' not found in {store.label}.")
    pending, seen = list(references(flow)), set()
    datasets, linked_services = [], []
    while pending:
        kind, name = pending.pop()
        if (kind, name) in seen or kind not in ("dataset", "linkedService"):
            continue
        seen.add((kind, name))
        definition = store.get(KINDS[kind], name)
        if definition is None:
            raise AdfMcpError("not_found", f"{kind} '{name}' used by dataflow '{dataflow}' is missing in {store.label}.")
        (datasets if kind == "dataset" else linked_services).append({"name": name, "properties": definition})
        pending.extend(references(definition))
    sources = [s.get("name") for s in (flow.get("typeProperties") or {}).get("sources") or []]
    package = {
        "dataFlow": {"name": dataflow, "properties": flow},
        "datasets": datasets,
        "linkedServices": linked_services,
        "debugSettings": {
            "sourceSettings": [{"sourceName": s, "rowLimit": row_limit} for s in sources],
            "parameters": {},
            "datasetParameters": {},
        },
    }
    return package, sources


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=RUN)
    @handle_errors
    def adf_pipeline_run(
        pipeline: str,
        parameters: dict | None = None,
        wait_seconds: int = 0,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Trigger a run of the published pipeline. wait_seconds>0 waits (at most ~50s per call) for the outcome;
        if it is still running, poll with adf_run_wait.
        Git factories: warns if the Git working-tree version differs from what will run (use adf_pipeline_debug_run)."""
        session, target, client = ctx(factory_name, resource_group)
        check_write(target, "Run pipeline")
        out: dict = {}
        if authoring_mode(session, target) == "git":
            try:
                git_def = GitStore(git_repo(session, target)).get(KINDS["pipeline"], pipeline)
                live_def = LiveStore(session, target).get(KINDS["pipeline"], pipeline)
                if git_def is not None and not same_definition(git_def, live_def):
                    out["warning"] = "The Git working-tree version differs from the published one; this run uses the published version."
            except AdfMcpError:
                pass
        run_id = _start(client, target, pipeline, parameters)
        audit(target, "pipeline_run", f"pipeline/{pipeline}", {"run_id": run_id})
        out["run_id"] = run_id
        if wait_seconds > 0:
            out["run"] = _outcome(client, target, wait_for_run(client, target, run_id, bounded(wait_seconds)))
        return out

    @mcp.tool(annotations=RUN)
    @handle_errors
    def adf_run_rerun(
        run_id: str,
        mode: str = "from_failure",
        activity_name: str | None = None,
        parameters: dict | None = None,
        wait_seconds: int = 0,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Rerun a pipeline run in the same run group. mode: from_failure (skip activities that succeeded),
        from_activity (start at activity_name), full. Parameters default to the original run's."""
        session, target, client = ctx(factory_name, resource_group)
        check_write(target, "Rerun pipeline")
        original = get_run(client, target, run_id)
        options: dict = {"reference_pipeline_run_id": run_id, "is_recovery": True}
        if mode == "from_failure":
            options["start_from_failure"] = True
        elif mode == "from_activity":
            if not activity_name:
                raise AdfMcpError("bad_argument", "mode=from_activity needs activity_name.")
            options["start_activity_name"] = activity_name
        elif mode != "full":
            raise AdfMcpError("bad_argument", "mode must be from_failure, from_activity or full.")
        params = parameters if parameters is not None else original.get("parameters")
        new_id = _start(client, target, original["pipelineName"], params, **options)
        audit(target, "pipeline_rerun", f"pipeline/{original['pipelineName']}", {"of": run_id, "mode": mode, "run_id": new_id})
        out: dict = {"run_id": new_id, "rerun_of": run_id, "mode": mode, "pipeline": original["pipelineName"]}
        if wait_seconds > 0:
            out["run"] = _outcome(client, target, wait_for_run(client, target, new_id, bounded(wait_seconds)))
        return out

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_run_wait(
        run_id: str, timeout_seconds: int = 45, factory_name: str | None = None, resource_group: str | None = None
    ) -> dict:
        """Wait for a pipeline run to finish (polls every 5s, at most ~50s per call) and return its outcome.
        If it is still running the result says so; call again to keep waiting."""
        _, target, client = ctx(factory_name, resource_group)
        return _outcome(client, target, wait_for_run(client, target, run_id, bounded(timeout_seconds)))

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_diagnose_failure(run_id: str, factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Root-cause a failed run: follows Execute Pipeline chains down to the failing activities, classifies each
        error (network, auth, permission, not found, throttling, timeout, data/schema, expression, IR, Databricks...)
        with a fix hint, and lists the datasets, linked services and integration runtime each one used."""
        session, target, client = ctx(factory_name, resource_group)
        run = get_run(client, target, run_id)
        if run.get("status") != "Failed":
            return {"run": pipeline_run_view(run), "diagnosis": f"Run is {run.get('status')}, not Failed."}
        live, cache, git_cache = LiveStore(session, target), {}, {}
        causes = []
        for leaf_run, activity, chain in _leaf_failures(client, target, run, []):
            error = activity.get("error") or {}
            category, hint = classify(activity.get("activityType"), error.get("errorCode"), error.get("message"))
            output = activity.get("output") if isinstance(activity.get("output"), dict) else {}
            cause = {
                "pipeline_chain": " > ".join(chain),
                "run_id": leaf_run.get("runId"),
                **activity_run_view(activity, error_chars=1500),
                "category": category,
                "hint": hint,
            }
            try:
                leaf_pipeline = leaf_run.get("pipelineName") or ""
                if leaf_pipeline.startswith(DEBUG_PREFIX):
                    # Debug-run copies are deleted after the run: read the Git version they were made from.
                    cause["debug_copy_of"] = original_name(leaf_pipeline)
                    store = resolve_store(session, target, "auto").store
                    cause["uses"] = _involved(store, git_cache, cause["debug_copy_of"], activity.get("activityName"))
                else:
                    cause["uses"] = _involved(live, cache, leaf_pipeline, activity.get("activityName"))
            except AdfMcpError:
                pass
            for key in ("runPageUrl", "effectiveIntegrationRuntime", "executionDuration"):
                if output.get(key):
                    cause[key] = output[key]
            if output.get("errors"):
                cause["copy_errors"] = truncate(output["errors"], 1000)
            causes.append(cause)
        return {
            "run": pipeline_run_view(run, message_chars=500),
            "root_causes": causes,
            "next": "adf_activity_io for full input/output; after fixing, adf_run_rerun mode=from_failure.",
        }

    @mcp.tool(annotations=RUN)
    @handle_errors
    def adf_pipeline_debug_run(
        pipeline: str,
        parameters: dict | None = None,
        ref: str | None = None,
        properties: dict | None = None,
        with_dependencies: bool = False,
        wait_seconds: int = 45,
        run_wait_seconds: int = 1800,
        keep: bool = False,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Debug-run an UNPUBLISHED pipeline version: the Git working tree (or `ref`), or `properties` you pass.
        It is published under a temporary zz_mcp_debug_* name, run, and deleted when the run ends (keep=true keeps it).
        By default, referenced child pipelines, data flows, datasets and linked services run as published.
        with_dependencies=true also publishes temporary copies of every referenced resource that is new or changed
        in Git (references rewritten to the copies), so a whole branch change can be tested before merge.
        Runs as a background job: returns within wait_seconds (capped ~50s) with the result or a job_id for
        adf_job_status; the job keeps waiting up to run_wait_seconds and always cleans up the copies.
        Blocked on read-only/prod profiles."""
        session, target, client = ctx(factory_name, resource_group)
        check_write(target, "Debug run")
        live = LiveStore(session, target)
        root = ("pipeline", pipeline)
        if properties is not None:
            plan = TempPlan(root, [root], {root: properties})
            not_copied: list[str] = []
        else:
            if authoring_mode(session, target) != "git":
                raise AdfMcpError("bad_argument", "This factory isn't Git-connected: pass `properties`, or use adf_pipeline_run.")
            git = GitStore(git_repo(session, target), ref)
            if git.get(KINDS["pipeline"], pipeline) is None:
                raise AdfMcpError("not_found", f"pipeline '{pipeline}' not found in {git.label}.")
            needed, unresolved = debug_copies(git, live, root)
            if unresolved:
                raise AdfMcpError("unresolved_references", f"Can't debug-run: {unresolved} exist neither in Git nor live.")
            copies = needed if with_dependencies else [root]
            not_copied = [fmt(r) for r in needed if r not in copies]
            missing_live = [r for r in not_copied if live.get(KINDS[r.split(":")[0]], r.split(":", 1)[1]) is None]
            if missing_live:
                raise AdfMcpError(
                    "dependencies_not_published",
                    f"'{pipeline}' references {missing_live}, which exist only in Git.",
                    "Pass with_dependencies=true to publish temporary copies of them for this run.",
                )
            plan = TempPlan(root, copies, {r: git.get(KINDS[r[0]], r[1]) for r in copies})

        def job(j):
            out = execute(j, session, target, plan, parameters, run_wait_seconds, keep, "pipeline_debug_run")
            if not_copied:
                out["running_published_versions_of"] = not_copied
            return {"pipeline": pipeline, **out}

        return run_job(session.jobs, "debugrun", f"debug run of {pipeline}", job, wait_seconds)

    @mcp.tool(annotations=RUN)
    @handle_errors
    def adf_linked_service_test(
        name: str,
        path: str | None = None,
        linked_service_parameters: dict | None = None,
        source: Source = "auto",
        ref: str | None = None,
        wait_seconds: int = 45,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Test a linked service's connection (ADF Studio's "Test connection" has no public API).

        Runs a tiny probe pipeline as a temporary debug run: `SELECT 1` for SQL-family stores, GetMetadata(exists)
        on `path` for storage (path starts with the container/file system, e.g. 'raw' or 'raw/in'), an
        authenticated GET for Key Vault / Databricks (managed identity) / REST; otherwise through a dataset that
        uses it. Works on the unpublished Git version too. Returns connected true/false with the classified error.
        """
        session, target, client = ctx(factory_name, resource_group)
        check_write(target, "Linked service test")
        resolved = resolve_store(session, target, source, ref)
        store, live = resolved.store, LiveStore(session, target)
        ls = (KINDS["linkedService"], name)
        definition = store.get(*ls)
        if definition is None:
            raise AdfMcpError("not_found", f"linkedService '{name}' not found in {store.label}.")
        datasets_using = sorted(
            ds for ds, d in store.list_definitions(KINDS["dataset"]).items() if ("linkedService", name) in references(d)
        )
        probe = build_probe(name, definition, path, linked_service_parameters, datasets_using)

        # What must be published temporarily: changed Git versions of the linked service (and what it uses).
        root = ("pipeline", f"probe_{name}")
        if isinstance(store, GitStore):
            start = ("dataset", probe.existing_dataset) if probe.existing_dataset else ("linkedService", name)
            copies, unresolved = debug_copies(store, live, start)
            if unresolved:
                raise AdfMcpError("unresolved_references", f"Can't test: {unresolved} exist neither in Git nor live.")
            if not probe.existing_dataset and same_definition(definition, live.get(*ls)) and copies == [("linkedService", name)]:
                copies = []  # published version is identical: probe it directly
            definitions = {r: store.get(KINDS[r[0]], r[1]) for r in copies}
        else:
            copies, definitions = [], {}
        if probe.dataset is not None:
            copies.append(("dataset", PROBE_DATASET))
            definitions[("dataset", PROBE_DATASET)] = probe.dataset
        copies.append(root)
        definitions[root] = probe.pipeline
        plan = TempPlan(root, copies, definitions)

        def job(j):
            j.log(f"probe: {probe.description}")
            result = execute(j, session, target, plan, None, 600, False, "linked_service_test")
            run = result["run"]
            out = {
                "linked_service": name,
                "type": definition.get("type"),
                "source": store.label,
                "probe": probe.description,
                "connected": run.get("status") == "Succeeded",
                "status": run.get("status"),
                "duration": run.get("duration"),
                "run_id": result["run_id"],
            }
            if run.get("status") == "Succeeded":
                activity = query_activity_runs(client, target, {"runId": result["run_id"], "runStart": None, "runEnd": None}, limit=5)[0]
                output = (activity[0].get("output") if activity else None) or {}
                out["result"] = truncate({k: v for k, v in output.items() if k not in ("billingReference", "durationInQueue", "effectiveIntegrationRuntime")}, 800)
                if isinstance(output, dict) and output.get("effectiveIntegrationRuntime"):
                    out["integration_runtime"] = output["effectiveIntegrationRuntime"]
            else:
                failed = (run.get("failed_activities") or [{}])[0]
                error = failed.get("error") or {}
                out["error"] = error
                out["category"], out["hint"] = classify(failed.get("type"), error.get("code"), error.get("message") or run.get("message"))
            if probe.notes:
                out["notes"] = probe.notes
            return out

        return run_job(session.jobs, "lstest", f"test linked service {name}", job, wait_seconds)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
    @handle_errors
    def adf_debug_cleanup(factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Delete temporary zz_mcp_debug_* resources left by adf_pipeline_debug_run (pipelines only once their runs
        have finished; then data flows, datasets and linked services)."""
        session, target, client = ctx(factory_name, resource_group)
        check_write(target, "Debug cleanup")
        live = LiveStore(session, target)
        temps = [n for n in live.list(KINDS["pipeline"]) if n.startswith(DEBUG_PREFIX)]
        deleted, busy = [], []
        for name in temps:
            body = {
                "lastUpdatedAfter": "2000-01-01T00:00:00Z",
                "lastUpdatedBefore": "2100-01-01T00:00:00Z",
                "filters": [
                    {"operand": "PipelineName", "operator": "Equals", "values": [name]},
                    {"operand": "Status", "operator": "In", "values": ["Queued", "InProgress", "Canceling"]},
                ],
            }
            if client.pipeline_runs.query_by_factory(target.resource_group, target.factory_name, body).value:
                busy.append(name)
                continue
            client.pipelines.delete(target.resource_group, target.factory_name, name)
            deleted.append(f"pipeline:{name}")
        if not busy:  # other temp copies may still be used by a running temp pipeline
            for kind in ("dataflow", "dataset", "linkedService"):
                for name in live.list(KINDS[kind]):
                    if name.startswith(DEBUG_PREFIX):
                        live.delete(KINDS[kind], name)
                        deleted.append(f"{kind}:{name}")
        if deleted:
            audit(target, "debug_cleanup", "pipelines", {"deleted": deleted})
        return {"deleted": deleted, "still_running": busy}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    @handle_errors
    def adf_dataflow_debug_session(
        action: str = "list",
        session_id: str | None = None,
        core_count: int = 8,
        time_to_live_minutes: int = 10,
        wait_seconds: int = 45,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Manage mapping data flow debug sessions. action: list | start | stop.
        start spins up Spark compute (billed; usually 3-6 minutes) with the given TTL and returns the session id
        when ready, or 'starting' — then call list until it appears. stop needs session_id."""
        session, target, client = ctx(factory_name, resource_group)
        rg, factory = target.resource_group, target.factory_name
        pending = session.cache.setdefault("dataflow_debug_pollers", [])
        if action == "start":
            check_write(target, "Start data flow debug session")
            poller = client.data_flow_debug_session.begin_create(
                rg, factory, {"computeType": "General", "coreCount": core_count, "timeToLive": time_to_live_minutes}
            )
            pending.append(poller)
            audit(target, "dataflow_debug_start", "dataflowDebugSession", {"core_count": core_count, "ttl": time_to_live_minutes})
            poller.wait(bounded(wait_seconds))
            if poller.done():
                return {"status": "ready", **poller.result().as_dict()}
            return {"status": "starting", "next": "Call adf_dataflow_debug_session action=list in a minute or two."}
        if action == "list":
            sessions = [s.as_dict() for s in client.data_flow_debug_session.query_by_factory(rg, factory)]
            for poller in list(pending):
                if poller.done():
                    pending.remove(poller)
            return {
                "sessions": [
                    {k: s.get(k) for k in ("sessionId", "computeType", "coreCount", "startTime", "lastActivityTime", "timeToLiveInMinutes", "dataFlowName")}
                    for s in sessions
                ],
                "starting": len(pending),
            }
        if action == "stop":
            if not session_id:
                raise AdfMcpError("bad_argument", "action=stop needs session_id.")
            client.data_flow_debug_session.delete(rg, factory, {"sessionId": session_id})
            audit(target, "dataflow_debug_stop", f"dataflowDebugSession/{session_id}")
            return {"stopped": session_id}
        raise AdfMcpError("bad_argument", "action must be list, start or stop.")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    @handle_errors
    def adf_dataflow_debug_preview(
        session_id: str,
        dataflow: str,
        stream_name: str | None = None,
        row_limit: int = 100,
        source: Source = "auto",
        ref: str | None = None,
        max_chars: int = 6000,
        wait_seconds: int = 45,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Preview rows of a data flow stream (a source or transformation name; default: first source) in a running
        debug session. Uses the data flow, datasets and linked services from `source` — so unpublished Git
        versions can be previewed. Runs as a background job (previews often take 1-2 minutes)."""
        session, target, client = ctx(factory_name, resource_group)
        resolved = resolve_store(session, target, source, ref)
        package, sources = _debug_package(resolved.store, dataflow, row_limit)
        package["sessionId"] = session_id
        rg, factory = target.resource_group, target.factory_name
        try:
            client.data_flow_debug_session.add_data_flow(rg, factory, package)
        except ResourceNotFoundError as exc:
            raise AdfMcpError("not_found", f"Debug session {session_id} not found or expired.", "Start one with adf_dataflow_debug_session.") from exc
        stream = stream_name or (sources[0] if sources else None)
        if not stream:
            raise AdfMcpError("bad_argument", "The data flow has no sources; pass stream_name.")

        def job(j):
            started = time.monotonic()
            j.log(f"previewing stream {stream}")
            response = client.data_flow_debug_session.begin_execute_command(
                rg,
                factory,
                {"sessionId": session_id, "command": "executePreviewQuery", "commandPayload": {"streamName": stream, "rowLimits": row_limit}},
            ).result().as_dict()
            return _preview_result(response, resolved.store.label, stream, time.monotonic() - started, max_chars)

        return run_job(session.jobs, "dfpreview", f"preview {dataflow}.{stream}", job, wait_seconds)


def _preview_result(response: dict, label: str, stream: str, seconds: float, max_chars: int) -> dict:
    data = response.get("data")
    try:
        data = json.loads(data) if isinstance(data, str) else data
    except json.JSONDecodeError:
        pass
    out = {
        "source": label,
        "stream": stream,
        "status": response.get("status"),
        "seconds": round(seconds, 1),
        "data": truncate(data, max_chars),
    }
    if response.get("status") != "Succeeded" and not data:
        # ADF returns no error details for failed previews.
        out["hint"] = (
            "ADF gave no error details. Usual causes: the source is unreachable from the debug IR (network/firewall/DNS), "
            "the debug IR's identity lacks access, or the stream name is wrong. Test the source with a pipeline "
            "(e.g. GetMetadata via adf_pipeline_debug_run) to get the real error."
        )
    return out
