"""Token economy: turn ARM payloads into compact views and paginate run queries."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import AdfMcpError

MAX_LIMIT = 500


def truncate(value: Any, max_chars: int) -> Any:
    """Return value unchanged if short, else a truncated string (JSON-encoded for non-strings)."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= max_chars:
        return value
    return text[:max_chars] + f"... [truncated, {len(text)} chars total]"


def duration(ms: int | None) -> str | None:
    if ms is None or ms < 0:
        return None
    seconds = ms // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s" if hours else f"{minutes}m{secs:02d}s"


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdfMcpError("bad_argument", f"Not an ISO-8601 timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def time_window(hours_back: float, start: str | None, end: str | None) -> tuple[str, str]:
    end_dt = parse_time(end) if end else datetime.now(UTC)
    start_dt = parse_time(start) if start else end_dt - timedelta(hours=hours_back)
    if start_dt >= end_dt:
        raise AdfMcpError("bad_argument", "start must be before end.")
    if end_dt - start_dt > timedelta(days=45):
        # ADF keeps run history for 45 days; wider windows only add load.
        start_dt = end_dt - timedelta(days=45)
    return start_dt.isoformat(), end_dt.isoformat()


def run_filters(**equals: str | list[str] | None) -> list[dict]:
    """Build RunQueryFilter dicts; list values become In filters. Keys are ADF operand names."""
    filters = []
    for operand, value in equals.items():
        if value is None or value == []:
            continue
        values = value if isinstance(value, list) else [value]
        filters.append({"operand": operand, "operator": "In" if len(values) > 1 else "Equals", "values": values})
    return filters


def paginate(query: Callable[[dict], Any], body: dict, limit: int) -> tuple[list[dict], bool]:
    """Run a query*/queryByFactory call until `limit` items; returns (items, more_available)."""
    limit = max(1, min(limit, MAX_LIMIT))
    items: list[dict] = []
    token = None
    while True:
        page = query({**body, **({"continuationToken": token} if token else {})})
        items.extend(item.as_dict() for item in page.value or [])
        token = page.continuation_token
        if len(items) >= limit or not token:
            return items[:limit], bool(token) or len(items) > limit


def pipeline_run_view(run: dict, message_chars: int = 300) -> dict:
    invoked = run.get("invokedBy") or {}
    # For child runs ADF puts the parent's activity run id in `name`; the parent pipeline is more useful.
    invoker = invoked.get("pipelineName") or invoked.get("name")
    view = {
        "run_id": run.get("runId"),
        "pipeline": run.get("pipelineName"),
        "status": run.get("status"),
        "start": run.get("runStart"),
        "end": run.get("runEnd"),
        "duration": duration(run.get("durationInMs")),
        "invoked_by": f"{invoker} ({invoked.get('invokedByType')})" if invoked else None,
    }
    if invoked.get("pipelineRunId"):
        view["parent_run_id"] = invoked["pipelineRunId"]
    if run.get("message"):
        view["message"] = truncate(run["message"], message_chars)
    if run.get("isLatest") is False:
        view["superseded_by_rerun"] = True
    return {k: v for k, v in view.items() if v is not None}


def activity_run_view(run: dict, error_chars: int = 500) -> dict:
    error = run.get("error") or {}
    output = run.get("output") if isinstance(run.get("output"), dict) else {}
    view = {
        "activity": run.get("activityName"),
        "type": run.get("activityType"),
        "status": run.get("status"),
        "start": run.get("activityRunStart"),
        "duration": duration(run.get("durationInMs")),
        "activity_run_id": run.get("activityRunId"),
    }
    if error.get("message") or error.get("errorCode"):
        view["error"] = {
            "code": error.get("errorCode"),
            "failure_type": error.get("failureType"),
            "message": truncate(error.get("message"), error_chars),
        }
    if output.get("pipelineRunId"):
        view["child_run_id"] = output["pipelineRunId"]
    return {k: v for k, v in view.items() if v not in (None, "")}


def trigger_run_view(run: dict, message_chars: int = 300) -> dict:
    view = {
        "trigger": run.get("triggerName"),
        "type": run.get("triggerType"),
        "status": run.get("status"),
        "at": run.get("triggerRunTimestamp"),
        "trigger_run_id": run.get("triggerRunId"),
        "pipeline_runs": run.get("triggeredPipelines"),
    }
    if run.get("message"):
        view["message"] = truncate(run["message"], message_chars)
    return {k: v for k, v in view.items() if v}
