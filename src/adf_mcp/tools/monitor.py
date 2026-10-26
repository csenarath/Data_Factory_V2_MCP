"""Toolset `monitor`: pipeline / activity / trigger runs, failure triage and run trees."""

from __future__ import annotations

from collections import Counter

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..compact import (
    activity_run_view,
    paginate,
    pipeline_run_view,
    run_filters,
    time_window,
    trigger_run_view,
    truncate,
)
from ..errors import AdfMcpError, handle_errors
from ..guards import audit, check_write
from ._common import ctx, failed_activities, get_run, query_activity_runs, query_pipeline_runs

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
MAX_TREE_NODES = 100


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_runs_query(
        hours_back: float = 24,
        start: str | None = None,
        end: str | None = None,
        pipeline_name: str | None = None,
        status: list[str] | None = None,
        latest_only: bool = False,
        limit: int = 50,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """List pipeline runs updated in a time window (default: last 24h), newest first.

        status: any of Queued, InProgress, Succeeded, Failed, Canceling, Cancelled.
        latest_only hides runs superseded by a rerun.
        start/end are ISO-8601 and override hours_back. History is kept 45 days.
        """
        _, target, client = ctx(factory_name, resource_group)
        after, before = time_window(hours_back, start, end)
        filters = run_filters(PipelineName=pipeline_name, Status=status)
        if latest_only:
            filters.append({"operand": "LatestOnly", "operator": "Equals", "values": ["true"]})
        body = {
            "lastUpdatedAfter": after,
            "lastUpdatedBefore": before,
            "filters": filters,
            "orderBy": [{"orderBy": "RunStart", "order": "DESC"}],
        }
        runs, more = query_pipeline_runs(client, target, body, limit)
        return {
            "factory": target.factory_name,
            "window": {"from": after, "to": before},
            "count": len(runs),
            "more_available": more,
            "status_counts": dict(Counter(r.get("status") for r in runs)),
            "runs": [pipeline_run_view(r) for r in runs],
        }

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_run_get(run_id: str, factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Get one pipeline run: status, parameters, full error message and its failed activities."""
        _, target, client = ctx(factory_name, resource_group)
        run = get_run(client, target, run_id)
        view = pipeline_run_view(run, message_chars=4000)
        view["parameters"] = run.get("parameters")
        view["run_group_id"] = run.get("runGroupId")
        view["is_latest"] = run.get("isLatest")
        if run.get("status") == "Failed":
            view["failed_activities"] = failed_activities(client, target, run)
        return view

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_activity_runs(
        run_id: str,
        status: list[str] | None = None,
        activity_name: str | None = None,
        activity_type: str | None = None,
        limit: int = 100,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """List activity runs of a pipeline run in execution order (compact; use adf_activity_io for inputs/outputs)."""
        _, target, client = ctx(factory_name, resource_group)
        run = get_run(client, target, run_id)
        items, more = query_activity_runs(
            client, target, run, limit, Status=status, ActivityName=activity_name, ActivityType=activity_type
        )
        return {
            "run": pipeline_run_view(run),
            "count": len(items),
            "more_available": more,
            "status_counts": dict(Counter(a.get("status") for a in items)),
            "activities": [activity_run_view(a) for a in items],
        }

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_activity_io(
        run_id: str,
        activity_name: str | None = None,
        activity_run_id: str | None = None,
        max_chars: int = 4000,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Show the input, output and error of one activity run (the last attempt if it retried). Large payloads are truncated."""
        if not activity_name and not activity_run_id:
            raise AdfMcpError("bad_argument", "Pass activity_name or activity_run_id.")
        _, target, client = ctx(factory_name, resource_group)
        run = get_run(client, target, run_id)
        items, _ = query_activity_runs(client, target, run, limit=500, ActivityName=activity_name)
        if activity_run_id:
            items = [a for a in items if a.get("activityRunId") == activity_run_id]
        if not items:
            raise AdfMcpError("not_found", "No matching activity run in this pipeline run.", "Call adf_activity_runs.")
        activity = items[-1]
        return {
            **activity_run_view(activity, error_chars=max_chars),
            "attempts": len(items),
            "input": truncate(activity.get("input"), max_chars),
            "output": truncate(activity.get("output"), max_chars),
            "user_properties": activity.get("userProperties") or None,
        }

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_trigger_runs(
        hours_back: float = 24,
        start: str | None = None,
        end: str | None = None,
        trigger_name: str | None = None,
        status: list[str] | None = None,
        limit: int = 50,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """List trigger runs (status Succeeded / Failed / Inprogress) in a time window, newest first."""
        _, target, client = ctx(factory_name, resource_group)
        after, before = time_window(hours_back, start, end)
        body = {
            "lastUpdatedAfter": after,
            "lastUpdatedBefore": before,
            "filters": run_filters(TriggerName=trigger_name, Status=status),
            "orderBy": [{"orderBy": "TriggerRunTimestamp", "order": "DESC"}],
        }
        runs, more = paginate(
            lambda b: client.trigger_runs.query_by_factory(target.resource_group, target.factory_name, b), body, limit
        )
        return {
            "factory": target.factory_name,
            "window": {"from": after, "to": before},
            "count": len(runs),
            "more_available": more,
            "status_counts": dict(Counter(r.get("status") for r in runs)),
            "trigger_runs": [trigger_run_view(r) for r in runs],
        }

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_failures_summary(
        hours_back: float = 24,
        top_pipelines: int = 10,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Triage failures in a window: failed pipelines ranked by failure count, whether each has since recovered,
        and the failing activities with error codes for the latest failure of each. Parents that failed only
        because a child pipeline failed are flagged (failed_because_of_child_runs) and ranked after root causes."""
        _, target, client = ctx(factory_name, resource_group)
        after, before = time_window(hours_back, None, None)
        body = {"lastUpdatedAfter": after, "lastUpdatedBefore": before, "orderBy": [{"orderBy": "RunStart", "order": "DESC"}]}
        runs, more = query_pipeline_runs(client, target, body, 500)

        by_pipeline: dict[str, list[dict]] = {}
        for run in runs:  # newest first
            by_pipeline.setdefault(run.get("pipelineName"), []).append(run)

        failing = []
        for pipeline, pipeline_runs in by_pipeline.items():
            failures = [r for r in pipeline_runs if r.get("status") == "Failed"]
            if not failures:
                continue
            latest = pipeline_runs[0]
            failing.append(
                {
                    "pipeline": pipeline,
                    "failed": len(failures),
                    "total_runs": len(pipeline_runs),
                    "latest_status": latest.get("status"),
                    "recovered": latest.get("status") == "Succeeded",
                    "latest_failure": failures[0],
                }
            )
        failing.sort(key=lambda f: (f["recovered"], -f["failed"]))

        for item in failing[:top_pipelines]:
            run = item.pop("latest_failure")
            item["latest_failure"] = pipeline_run_view(run, message_chars=400)
            item["failed_activities"] = failed_activities(client, target, run, limit=5)
            # A parent that failed only because an Execute Pipeline child failed is not a root cause.
            child_ids = [a.get("child_run_id") for a in item["failed_activities"]]
            if child_ids and all(child_ids):
                item["failed_because_of_child_runs"] = child_ids
        for item in failing[top_pipelines:]:
            item["latest_failure"] = {"run_id": item.pop("latest_failure").get("runId")}
        failing.sort(key=lambda f: (f["recovered"], "failed_because_of_child_runs" in f, -f["failed"]))

        return {
            "factory": target.factory_name,
            "window": {"from": after, "to": before},
            "runs_scanned": len(runs),
            "scan_truncated": more,
            "status_counts": dict(Counter(r.get("status") for r in runs)),
            "failing_pipelines": len(failing),
            "still_failing": sum(1 for f in failing if not f["recovered"]),
            "pipelines": failing,
        }

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_run_tree(
        run_id: str,
        max_depth: int = 5,
        include_root: bool = True,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """End-to-end view of a run across Execute Pipeline chains: walks up to the root run (include_root),
        then down through every child run, with status and failed activities at each level."""
        _, target, client = ctx(factory_name, resource_group)
        run = get_run(client, target, run_id)
        if include_root:
            for _ in range(10):
                parent_id = (run.get("invokedBy") or {}).get("pipelineRunId")
                if not parent_id or parent_id == run.get("runId"):
                    break
                run = get_run(client, target, parent_id)

        visited = {"count": 0}

        def node(current: dict, depth: int) -> dict:
            visited["count"] += 1
            view = pipeline_run_view(current, message_chars=300)
            if current.get("runId") == run_id:
                view["requested"] = True
            activities, _ = query_activity_runs(client, target, current, limit=500)
            failed = [activity_run_view(a, error_chars=300) for a in activities if a.get("status") == "Failed"]
            if failed:
                view["failed_activities"] = failed
            view["activities"] = dict(Counter(a.get("status") for a in activities))
            child_ids = [
                (a.get("activityName"), (a.get("output") or {}).get("pipelineRunId"))
                for a in activities
                if a.get("activityType") == "ExecutePipeline" and isinstance(a.get("output"), dict)
            ]
            children = []
            for activity_name, child_id in child_ids:
                if not child_id:
                    continue
                if depth >= max_depth or visited["count"] >= MAX_TREE_NODES:
                    children.append({"via_activity": activity_name, "run_id": child_id, "not_expanded": True})
                    continue
                child = node(get_run(client, target, child_id), depth + 1)
                children.append({"via_activity": activity_name, **child})
            if children:
                view["children"] = children
            return view

        tree = node(run, 0)
        return {"factory": target.factory_name, "runs_in_tree": visited["count"], "tree": tree}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
    @handle_errors
    def adf_run_cancel(
        run_id: str, recursive: bool = True, factory_name: str | None = None, resource_group: str | None = None
    ) -> dict:
        """Cancel an in-progress pipeline run (recursive also cancels child runs). Blocked on read-only / prod profiles."""
        _, target, client = ctx(factory_name, resource_group)
        check_write(target, "Cancel run")
        run = get_run(client, target, run_id)
        if run.get("status") not in ("Queued", "InProgress"):
            raise AdfMcpError("bad_state", f"Run is {run.get('status')}; only Queued or InProgress runs can be cancelled.")
        client.pipeline_runs.cancel(target.resource_group, target.factory_name, run_id, is_recursive=recursive)
        audit(target, "run_cancel", f"pipelineRun/{run_id}", {"pipeline": run.get("pipelineName"), "recursive": recursive})
        return {"status": "cancel_requested", "run_id": run_id, "pipeline": run.get("pipelineName")}
