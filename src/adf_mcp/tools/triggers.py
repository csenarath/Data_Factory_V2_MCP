"""Toolset `triggers`: runtime control of triggers (definitions are changed via adf_resource_upsert)."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..errors import AdfMcpError, handle_errors
from ..guards import audit, authoring_mode, check_write
from ..resources import KINDS, same_definition
from ..stores import GitStore, git_repo
from ._common import ctx

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
RUNTIME = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
EVENT_TRIGGERS = {"BlobEventsTrigger", "CustomEventsTrigger"}


def _schedule(props: dict) -> str | None:
    tp = props.get("typeProperties") or {}
    rec = tp.get("recurrence") or (tp if props.get("type") == "TumblingWindowTrigger" else None)
    if rec:
        text = f"every {rec.get('interval')} {rec.get('frequency')}"
        if rec.get("schedule"):
            text += f" at {rec['schedule']}"
        return text + (f" ({rec['timeZone']})" if rec.get("timeZone") else "")
    if props.get("type") in EVENT_TRIGGERS:
        return f"on {', '.join(tp.get('events') or [])} {tp.get('blobPathBeginsWith') or ''}".strip()
    return None


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_triggers_list(factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """List live triggers with runtime state, schedule and pipelines. Git factories also flag triggers whose
        Git definition (collaboration branch) differs from the published one."""
        session, target, client = ctx(factory_name, resource_group)
        git_store = None
        if authoring_mode(session, target) == "git":
            try:
                repo = git_repo(session, target)
                git_store = GitStore(repo, repo.base_ref())
            except AdfMcpError:
                git_store = None
        rows = []
        for item in client.triggers.list_by_factory(target.resource_group, target.factory_name):
            data = item.as_dict()
            props = data.get("properties") or {}
            pipelines = [p.get("pipelineReference", {}).get("referenceName") for p in props.get("pipelines") or []]
            if props.get("pipeline"):
                pipelines.append(props["pipeline"].get("pipelineReference", {}).get("referenceName"))
            row = {
                "name": data["name"],
                "type": props.get("type"),
                "runtime_state": props.get("runtimeState"),
                "schedule": _schedule(props),
                "pipelines": pipelines,
            }
            if git_store is not None:
                git_def = git_store.get(KINDS["trigger"], data["name"])
                strip = lambda d: {k: v for k, v in (d or {}).items() if k != "runtimeState"}  # noqa: E731
                row["git"] = "missing" if git_def is None else ("same" if same_definition(strip(git_def), strip(props)) else "differs")
            rows.append(row)
        return {"factory": target.factory_name, "count": len(rows), "triggers": rows}

    @mcp.tool(annotations=RUNTIME)
    @handle_errors
    def adf_trigger_start(name: str, factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Start a published trigger. Storage/custom event triggers are subscribed to Event Grid first if needed."""
        session, target, client = ctx(factory_name, resource_group)
        check_write(target, "Start trigger")
        rg, factory = target.resource_group, target.factory_name
        props = client.triggers.get(rg, factory, name).as_dict().get("properties") or {}
        subscription = None
        if props.get("type") in EVENT_TRIGGERS:
            status = client.triggers.get_event_subscription_status(rg, factory, name).as_dict().get("status")
            if status != "Enabled":
                subscription = client.triggers.begin_subscribe_to_events(rg, factory, name).result().as_dict().get("status")
        client.triggers.begin_start(rg, factory, name).result()
        audit(target, "trigger_start", f"trigger/{name}")
        state = client.triggers.get(rg, factory, name).as_dict()["properties"].get("runtimeState")
        return {"trigger": name, "runtime_state": state, **({"event_subscription": subscription} if subscription else {})}

    @mcp.tool(annotations=RUNTIME)
    @handle_errors
    def adf_trigger_stop(name: str, factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Stop a published trigger (scheduled runs stop; runs already in progress continue)."""
        session, target, client = ctx(factory_name, resource_group)
        check_write(target, "Stop trigger")
        rg, factory = target.resource_group, target.factory_name
        client.triggers.begin_stop(rg, factory, name).result()
        audit(target, "trigger_stop", f"trigger/{name}")
        state = client.triggers.get(rg, factory, name).as_dict()["properties"].get("runtimeState")
        return {"trigger": name, "runtime_state": state}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    @handle_errors
    def adf_trigger_rerun(
        trigger_name: str, trigger_run_id: str, factory_name: str | None = None, resource_group: str | None = None
    ) -> dict:
        """Rerun a trigger run (tumbling window triggers), e.g. to re-process a failed window. IDs come from adf_trigger_runs."""
        session, target, client = ctx(factory_name, resource_group)
        check_write(target, "Rerun trigger run")
        client.trigger_runs.rerun(target.resource_group, target.factory_name, trigger_name, trigger_run_id)
        audit(target, "trigger_rerun", f"trigger/{trigger_name}/{trigger_run_id}")
        return {"trigger": trigger_name, "rerun_of": trigger_run_id, "status": "rerun_requested"}
