"""Toolset `debug`: run / rerun pipelines, wait, diagnose failures, debug-run unpublished Git versions,
and mapping data flow debug sessions.

ADF has no public API for ADF Studio's Debug button, and runs always execute the *published* definition.
adf_pipeline_debug_run bridges that: it publishes the Git (or supplied) version under a temporary
`zz_mcp_debug_*` name, runs it, and deletes it afterwards.
"""

from __future__ import annotations

import json
import secrets
import time

from azure.core.exceptions import ResourceNotFoundError
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..checks import fmt
from ..compact import activity_run_view, pipeline_run_view, truncate
from ..diagnose import classify
from ..errors import AdfMcpError, handle_errors
from ..guards import audit, authoring_mode, check_write
from ..resources import KINDS, REFERENCE_TYPES, find_activity, references, same_definition
from ..stores import GitStore, LiveStore, Source, git_repo, resolve_store
from ._common import DEBUG_PREFIX, TERMINAL, ctx, failed_activities, get_run, query_activity_runs, wait_for_run

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
RUN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
DEBUG_FOLDER = "_mcp_debug"


def _outcome(client, target, run: dict) -> dict:
    view = pipeline_run_view(run, message_chars=1000)
    if run.get("status") == "Failed":
        view["failed_activities"] = failed_activities(client, target, run)
    elif run.get("status") not in TERMINAL:
        view["still_running"] = True
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


def _involved(live: LiveStore, cache: dict, pipeline: str, activity_name: str) -> dict:
    """Datasets, linked services and IRs an activity uses, from the published definition."""
    key = ("pipeline", pipeline)
    if key not in cache:
        cache[key] = live.get(KINDS["pipeline"], pipeline) or {}
    activity = find_activity(cache[key], activity_name) or {}
    refs = set(references(activity))
    for kind, name in list(refs):
        if kind == "dataset":
            cache.setdefault(("dataset", name), live.get(KINDS["dataset"], name) or {})
            refs |= references(cache[("dataset", name)])
    for kind, name in list(refs):
        if kind == "linkedService":
            cache.setdefault(("linkedService", name), live.get(KINDS["linkedService"], name) or {})
            ls = cache[("linkedService", name)]
            refs |= references(ls)
            refs.add(("integrationRuntime", (ls.get("connectVia") or {}).get("referenceName") or "AutoResolveIntegrationRuntime"))
    return {"resources": sorted(fmt(r) for r in refs if r[0] != "pipeline"), "policy": activity.get("policy")}


COPYABLE = ("pipeline", "dataflow", "dataset", "linkedService")  # IRs and credentials must already be published


def _debug_copies(git: GitStore, live: LiveStore, root: tuple[str, str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Resources that need a temporary published copy for `root` to run as it is in Git.

    A resource needs a copy if its Git version is missing in or differs from live, or if anything it
    references needs a copy (so the copy can point at the other copies). Returns them dependencies-first,
    plus references that can't be satisfied (missing in both Git and live, or non-copyable kinds missing live).
    """
    need: dict[tuple[str, str], bool] = {}
    order: list[tuple[str, str]] = []
    unresolved: list[str] = []

    def visit(ref: tuple[str, str], stack: frozenset) -> bool:
        if ref in need:
            return need[ref]
        if ref in stack:  # reference cycle: treat as not needing a copy
            return False
        kind, name = ref
        if kind not in COPYABLE:
            if ref != ("integrationRuntime", "AutoResolveIntegrationRuntime") and live.get(KINDS[kind], name) is None:
                unresolved.append(f"{kind}:{name}")
            need[ref] = False
            return False
        git_def = git.get(KINDS[kind], name)
        if git_def is None:
            if live.get(KINDS[kind], name) is None:
                unresolved.append(f"{kind}:{name}")
            need[ref] = False
            return False
        children = [visit(child, stack | {ref}) for child in sorted(references(git_def))]
        changed = ref == root or not same_definition(git_def, live.get(KINDS[kind], name))
        need[ref] = changed or any(children)
        if need[ref]:
            order.append(ref)
        return need[ref]

    visit(root, frozenset())
    return order, unresolved


def _rewrite(definition, renames: dict[tuple[str, str], str]):
    """Point references at temporary copies."""
    if isinstance(definition, dict):
        out = {k: _rewrite(v, renames) for k, v in definition.items()}
        kind = REFERENCE_TYPES.get(definition.get("type")) if isinstance(definition.get("type"), str) else None
        if kind and (kind, definition.get("referenceName")) in renames:
            out["referenceName"] = renames[(kind, definition["referenceName"])]
        return out
    if isinstance(definition, list):
        return [_rewrite(v, renames) for v in definition]
    return definition


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
        """Trigger a run of the published pipeline. wait_seconds>0 waits for the outcome (with failed activities).
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
            out["run"] = _outcome(client, target, wait_for_run(client, target, run_id, wait_seconds))
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
            out["run"] = _outcome(client, target, wait_for_run(client, target, new_id, wait_seconds))
        return out

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_run_wait(
        run_id: str, timeout_seconds: int = 300, factory_name: str | None = None, resource_group: str | None = None
    ) -> dict:
        """Wait for a pipeline run to finish (polls every 5s up to timeout_seconds) and return its outcome."""
        _, target, client = ctx(factory_name, resource_group)
        return _outcome(client, target, wait_for_run(client, target, run_id, min(timeout_seconds, 3600)))

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
        live, cache = LiveStore(session, target), {}
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
                cause["uses"] = _involved(live, cache, leaf_run.get("pipelineName"), activity.get("activityName"))
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
        wait_seconds: int = 600,
        keep: bool = False,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Debug-run an UNPUBLISHED pipeline version: the Git working tree (or `ref`), or `properties` you pass.
        It is published under a temporary zz_mcp_debug_* name, run, and deleted when the run ends (keep=true keeps it).
        By default, referenced child pipelines, data flows, datasets and linked services run as published.
        with_dependencies=true also publishes temporary copies of every referenced resource that is new or changed
        in Git (references rewritten to the copies), so a whole branch change can be tested before merge.
        Blocked on read-only/prod profiles."""
        session, target, client = ctx(factory_name, resource_group)
        check_write(target, "Debug run")
        live = LiveStore(session, target)
        suffix = secrets.token_hex(3)
        root = ("pipeline", pipeline)
        temp_name = lambda name: f"{DEBUG_PREFIX}{name}"[:130] + f"_{suffix}"  # noqa: E731

        if properties is not None:
            plan, definitions, unresolved = [root], {root: properties}, []
        else:
            if authoring_mode(session, target) != "git":
                raise AdfMcpError("bad_argument", "This factory isn't Git-connected: pass `properties`, or use adf_pipeline_run.")
            git = GitStore(git_repo(session, target), ref)
            if git.get(KINDS["pipeline"], pipeline) is None:
                raise AdfMcpError("not_found", f"pipeline '{pipeline}' not found in {git.label}.")
            plan, unresolved = _debug_copies(git, live, root)
            if unresolved:
                raise AdfMcpError("unresolved_references", f"Can't debug-run: {unresolved} exist neither in Git nor live.")
            definitions = {ref_: git.get(KINDS[ref_[0]], ref_[1]) for ref_ in plan}
        copies = plan if with_dependencies else [root]
        not_copied = [fmt(r) for r in plan if r not in copies]
        if not with_dependencies:
            missing_live = [r for r in not_copied if live.get(KINDS[r.split(":")[0]], r.split(":", 1)[1]) is None]
            if missing_live:
                raise AdfMcpError(
                    "dependencies_not_published",
                    f"'{pipeline}' references {missing_live}, which exist only in Git.",
                    "Pass with_dependencies=true to publish temporary copies of them for this run.",
                )

        renames = {r: temp_name(r[1]) for r in copies}
        created: list[tuple[str, str]] = []
        out: dict = {"pipeline": pipeline}
        try:
            for ref_ in copies:  # dependencies first
                body = _rewrite(definitions[ref_], renames)
                if ref_[0] in ("pipeline", "dataflow", "dataset"):
                    body = {**body, "folder": {"name": DEBUG_FOLDER}}
                live.put(KINDS[ref_[0]], renames[ref_], body)
                created.append((ref_[0], renames[ref_]))
            run_id = _start(client, target, renames[root], parameters)
        except Exception:
            for kind, name in reversed(created):
                live.delete(KINDS[kind], name)
            raise
        audit(target, "pipeline_debug_run", f"pipeline/{pipeline}", {"temp_copies": [f"{k}:{n}" for k, n in created], "run_id": run_id})
        out.update(run_id=run_id, temp_copies=[f"{k}:{n}" for k, n in created])
        if not_copied:
            out["running_published_versions_of"] = not_copied
        run = wait_for_run(client, target, run_id, min(wait_seconds, 3600)) if wait_seconds > 0 else get_run(client, target, run_id)
        out["run"] = _outcome(client, target, run)
        if run.get("status") in TERMINAL and not keep:
            for kind, name in reversed(created):
                live.delete(KINDS[kind], name)
            out["temp_copies_deleted"] = True
        else:
            out["cleanup"] = "Call adf_debug_cleanup after the run finishes to delete the temporary copies."
        return out

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
        wait_seconds: int = 60,
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
            poller.wait(max(0, wait_seconds))
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
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Preview rows of a data flow stream (a source or transformation name; default: first source) in a running
        debug session. Uses the data flow, datasets and linked services from `source` — so unpublished Git
        versions can be previewed."""
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
        started = time.monotonic()
        response = client.data_flow_debug_session.begin_execute_command(
            rg,
            factory,
            {"sessionId": session_id, "command": "executePreviewQuery", "commandPayload": {"streamName": stream, "rowLimits": row_limit}},
        ).result().as_dict()
        data = response.get("data")
        try:
            data = json.loads(data) if isinstance(data, str) else data
        except json.JSONDecodeError:
            pass
        out = {
            "source": resolved.store.label,
            "stream": stream,
            "status": response.get("status"),
            "seconds": round(time.monotonic() - started, 1),
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
