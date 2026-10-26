import pytest

from adf_mcp.compact import paginate, time_window
from adf_mcp.config import Profile
from adf_mcp.session import set_session

from .conftest import FakeAdf, FakeSession, page

T0 = "2026-10-26T01:00:00Z"
T1 = "2026-10-26T01:10:00Z"


def run(run_id, pipeline, status, start=T0, parent=None, message=None):
    invoked = {"name": "Daily", "invokedByType": "ScheduleTrigger"}
    if parent:
        invoked = {"name": "ar-guid", "invokedByType": "PipelineActivity", "pipelineRunId": parent, "pipelineName": "pl_master"}
    return {
        "runId": run_id,
        "pipelineName": pipeline,
        "status": status,
        "runStart": start,
        "runEnd": T1,
        "durationInMs": 600_000,
        "invokedBy": invoked,
        "message": message,
    }


def activity(name, status, type_="Copy", child=None, error=None):
    data = {
        "activityName": name,
        "activityType": type_,
        "status": status,
        "activityRunStart": T0,
        "durationInMs": 1000,
        "activityRunId": f"ar-{name}",
        "input": {"source": "x" * 50},
        "output": {"pipelineRunId": child} if child else {"rowsCopied": 10},
    }
    if error:
        data["error"] = {"errorCode": "2200", "failureType": "UserError", "message": error}
    return data


@pytest.fixture
def factory(dev_profile):
    runs = [
        run("root", "pl_master", "Failed", message="child failed"),
        run("child", "pl_load_sales", "Failed", parent="root"),
        run("ok-1", "pl_load_sales", "Succeeded", start="2026-10-26T03:00:00Z"),
        run("bad-1", "pl_export", "Failed", start="2026-10-26T02:00:00Z", message="timeout"),
        run("live", "pl_export", "InProgress", start="2026-10-26T04:00:00Z"),
    ]
    activities = {
        "root": [activity("exec_sales", "Failed", "ExecutePipeline", child="child", error="child failed")],
        "child": [activity("copy_sales", "Failed", error="Login failed for user")],
        "bad-1": [activity("copy_export", "Failed", error="Operation timed out")],
    }
    adf = FakeAdf(runs, activities)
    set_session(FakeSession(dev_profile, adf))
    return adf


def test_runs_query_compacts_and_filters(factory, call):
    result = call("adf_runs_query", status=["Failed"])
    assert result["count"] == 3
    assert result["status_counts"] == {"Failed": 3}
    first = result["runs"][0]
    assert first["duration"] == "10m00s"
    assert "durationInMs" not in first
    child = next(r for r in result["runs"] if r["run_id"] == "child")
    assert child["parent_run_id"] == "root"
    assert child["invoked_by"] == "pl_master (PipelineActivity)"
    assert factory.queries[0]["filters"] == [{"operand": "Status", "operator": "Equals", "values": ["Failed"]}]


def test_run_get_includes_failed_activities(factory, call):
    result = call("adf_run_get", run_id="child")
    assert result["failed_activities"][0]["error"]["message"] == "Login failed for user"


def test_failures_summary_ranks_still_failing_first(factory, call):
    result = call("adf_failures_summary")
    assert result["failing_pipelines"] == 3
    by_name = {p["pipeline"]: p for p in result["pipelines"]}
    assert by_name["pl_load_sales"]["recovered"] is True  # later run succeeded
    assert by_name["pl_export"]["recovered"] is False
    assert by_name["pl_export"]["failed_activities"][0]["activity"] == "copy_export"
    # Root causes first, then parents that failed only via a child, then recovered pipelines.
    assert by_name["pl_master"]["failed_because_of_child_runs"] == ["child"]
    assert [p["pipeline"] for p in result["pipelines"]] == ["pl_export", "pl_master", "pl_load_sales"]


def test_run_tree_walks_up_then_down(factory, call):
    result = call("adf_run_tree", run_id="child")
    tree = result["tree"]
    assert tree["run_id"] == "root"
    assert result["runs_in_tree"] == 2
    child = tree["children"][0]
    assert child["via_activity"] == "exec_sales"
    assert child["requested"] is True
    assert child["failed_activities"][0]["error"]["code"] == "2200"


def test_activity_io_truncates(factory, call):
    result = call("adf_activity_io", run_id="child", activity_name="copy_sales", max_chars=20)
    assert "truncated" in result["input"]
    assert result["runs_with_this_name"] == 1


def test_cancel_only_in_progress_and_audited(factory, call, call_error, isolated_home):
    assert call_error("adf_run_cancel", run_id="ok-1")["code"] == "bad_state"
    assert call("adf_run_cancel", run_id="live")["status"] == "cancel_requested"
    assert factory.cancelled == [("live", True)]
    assert "pipelineRun/live" in (isolated_home / "audit.jsonl").read_text()


def test_cancel_blocked_on_prod(call_error):
    prod = Profile(name="prod", subscription_id="s", resource_group="rg", factory_name="f", environment="prod")
    set_session(FakeSession(prod, FakeAdf([run("live", "p", "InProgress")])))
    error = call_error("adf_run_cancel", run_id="live")
    assert error["code"] == "write_blocked"
    assert "hint" in error


def test_no_profile_gives_actionable_error(call_error):
    from adf_mcp.config import ProfilesFile
    from adf_mcp.session import Session

    set_session(Session(ProfilesFile()))
    error = call_error("adf_runs_query")
    assert error["code"] == "no_profile"


def test_paginate_follows_continuation_tokens():
    pages = {None: page([{"runId": "1"}, {"runId": "2"}], token="a"), "a": page([{"runId": "3"}], token=None)}
    items, more = paginate(lambda body: pages[body.get("continuationToken")], {}, limit=10)
    assert [i["runId"] for i in items] == ["1", "2", "3"] and more is False
    items, more = paginate(lambda body: pages[body.get("continuationToken")], {}, limit=2)
    assert len(items) == 2 and more is True


def test_time_window_caps_at_45_days():
    start, end = time_window(24 * 90, None, "2026-10-26T00:00:00Z")
    assert start.startswith("2026-09-11")


def test_failures_summary_excludes_debug_copies(factory, call):
    factory.runs["dbg"] = run("dbg", "zz_mcp_debug_pl_export_a1b2c3", "Failed", start="2026-10-26T05:00:00Z")
    result = call("adf_failures_summary")
    assert result["debug_runs_excluded"] == 1
    assert all(not p["pipeline"].startswith("zz_mcp_debug_") for p in result["pipelines"])


def test_original_name_of_debug_copy():
    from adf_mcp.tools.debug import original_name

    assert original_name("zz_mcp_debug_pl_load_sales_a1b2c3") == "pl_load_sales"
    assert original_name("pl_load_sales") == "pl_load_sales"
