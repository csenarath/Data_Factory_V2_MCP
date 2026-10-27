"""Helpers shared by toolsets: target resolution and run queries."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from azure.mgmt.datafactory import DataFactoryManagementClient

from ..compact import activity_run_view, paginate, parse_time, run_filters
from ..session import Session, Target, get_session

from ..tempdeploy import DEBUG_PREFIX, TERMINAL  # noqa: F401  (re-exported for toolsets)


def ctx(factory_name: str | None, resource_group: str | None) -> tuple[Session, Target, DataFactoryManagementClient]:
    session = get_session()
    target = session.target(factory_name, resource_group)
    return session, target, session.adf(target)


def get_run(client, target: Target, run_id: str) -> dict:
    return client.pipeline_runs.get(target.resource_group, target.factory_name, run_id).as_dict()


def query_pipeline_runs(client, target: Target, body: dict, limit: int) -> tuple[list[dict], bool]:
    return paginate(
        lambda b: client.pipeline_runs.query_by_factory(target.resource_group, target.factory_name, b), body, limit
    )


def query_activity_runs(
    client, target: Target, run: dict, limit: int = 200, **filters: str | list[str] | None
) -> tuple[list[dict], bool]:
    # Activity queries are bounded by last-updated time; bracket the whole pipeline run.
    start = parse_time(run["runStart"]) - timedelta(hours=1) if run.get("runStart") else datetime.now(UTC) - timedelta(days=1)
    end = parse_time(run["runEnd"]) + timedelta(hours=1) if run.get("runEnd") else datetime.now(UTC) + timedelta(hours=1)
    body = {
        "lastUpdatedAfter": start.isoformat(),
        "lastUpdatedBefore": end.isoformat(),
        "filters": run_filters(**filters),
        "orderBy": [{"orderBy": "ActivityRunStart", "order": "ASC"}],
    }
    return paginate(
        lambda b: client.activity_runs.query_by_pipeline_run(target.resource_group, target.factory_name, run["runId"], b),
        body,
        limit,
    )


def failed_activities(client, target: Target, run: dict, limit: int = 10) -> list[dict]:
    items, _ = query_activity_runs(client, target, run, limit=limit, Status="Failed")
    return [activity_run_view(a, error_chars=300) for a in items]


def wait_for_run(client, target: Target, run_id: str, timeout_seconds: float, poll_seconds: float = 5) -> dict:
    """Poll a pipeline run until it finishes or the timeout passes; returns the last run state."""
    deadline = time.monotonic() + max(0, timeout_seconds)
    while True:
        run = get_run(client, target, run_id)
        if run.get("status") in TERMINAL or time.monotonic() >= deadline:
            return run
        time.sleep(poll_seconds)
