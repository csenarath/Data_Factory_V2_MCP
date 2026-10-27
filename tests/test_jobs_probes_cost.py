import time

import pytest

from adf_mcp.errors import AdfMcpError
from adf_mcp.jobs import Jobs, bounded, run_job
from adf_mcp.pricing import FALLBACK, ir_prefix, meter_for
from adf_mcp.probes import PROBE_DATASET, build_probe
from adf_mcp.publishing import _topological
from adf_mcp.tools.cost import compute, recommendations


def test_run_job_returns_result_or_handle(monkeypatch):
    monkeypatch.setenv("ADF_MCP_MAX_WAIT_SECONDS", "0.3")
    jobs = Jobs()
    assert run_job(jobs, "t", "fast", lambda j: {"ok": 1}, 10)["ok"] == 1

    handle = run_job(jobs, "t", "slow", lambda j: (j.log("working"), time.sleep(1), {"done": True})[-1], 10)
    assert handle["status"] == "running" and "adf_job_status" in handle["next"]
    job = jobs.get(handle["job_id"])
    job.done.wait(3)
    assert job.view()["result"] == {"done": True} and "working" in job.view()["progress"][0]

    def boom(j):
        raise AdfMcpError("bad", "it broke")

    with pytest.raises(AdfMcpError):
        run_job(jobs, "t", "fails", boom, 10)
    assert bounded(999) == 0.3


def test_probes_per_linked_service_type():
    sql = build_probe("ls_sql", {"type": "AzureSqlDatabase", "typeProperties": {}})
    assert sql.dataset["type"] == "AzureSqlTable"
    activity = sql.pipeline["activities"][0]
    assert activity["type"] == "Lookup" and activity["typeProperties"]["source"]["sqlReaderQuery"] == "SELECT 1 AS ok"
    assert activity["typeProperties"]["dataset"]["referenceName"] == PROBE_DATASET

    blob = build_probe("ls_adls", {"type": "AzureBlobFS"}, path="raw/in/2026")
    assert blob.dataset["typeProperties"]["location"] == {"type": "AzureBlobFSLocation", "fileSystem": "raw", "folderPath": "in/2026"}
    with pytest.raises(AdfMcpError):
        build_probe("ls_adls", {"type": "AzureBlobFS"})  # needs a container
    via = build_probe("ls_adls", {"type": "AzureBlobFS"}, datasets_using=["ds1"])
    assert via.existing_dataset == "ds1"

    kv = build_probe("kv", {"type": "AzureKeyVault", "typeProperties": {"baseUrl": "https://v.vault.azure.net/"}})
    web = kv.pipeline["activities"][0]["typeProperties"]
    assert web["url"].startswith("https://v.vault.azure.net/secrets?") and web["authentication"]["resource"] == "https://vault.azure.net"
    with pytest.raises(AdfMcpError):
        build_probe("dbx", {"type": "AzureDatabricks", "typeProperties": {"domain": "https://x", "accessToken": {}}})
    with pytest.raises(AdfMcpError):
        build_probe("odd", {"type": "Marketo"})


def test_publish_orders_same_kind_dependencies_first():
    pipelines = {
        "parent": {"activities": [{"type": "ExecutePipeline", "typeProperties": {"pipeline": {"referenceName": "child", "type": "PipelineReference"}}}]},
        "child": {"activities": []},
        "solo": {"activities": []},
    }
    order = _topological("pipeline", ["parent", "child", "solo"], pipelines)
    assert order.index("child") < order.index("parent")
    linked = {"sql": {"typeProperties": {"password": {"type": "AzureKeyVaultSecret", "store": {"referenceName": "kv", "type": "LinkedServiceReference"}}}}, "kv": {}}
    assert _topological("linkedService", ["sql", "kv"], linked) == ["kv", "sql"]


def test_pricing_meters():
    assert meter_for("DataMovement", "AzureIR") == "Cloud Data Movement"
    assert meter_for("PipelineActivity", "SelfhostedIR") == "Self Hosted Pipeline Activity"
    assert meter_for("executedataflow", "General") == "Data Flow General Purpose"
    assert ir_prefix(None, "AutoResolveIntegrationRuntime (Southeast Asia)") == "Cloud"
    assert ir_prefix("ManagedVNetIR", None) == "Azure Managed VNET"


def _activity(type_, billing=None, **output):
    if billing:
        output["billingReference"] = billing
    return {"activityType": type_, "activityName": type_.lower(), "output": output}


def test_cost_compute_and_recommendations():
    copy_billing = {"activityType": "DataMovement", "billableDuration": [{"meterType": "AzureIR", "duration": 0.5, "unit": "DIUHours"}]}
    meta_billing = {"activityType": "PipelineActivity", "billableDuration": [{"meterType": "AzureIR", "duration": 0.0166, "unit": "Hours"}]}
    pairs = [
        ({"pipelineName": "p_copy", "status": "Succeeded"}, [_activity("Copy", copy_billing, usedDataIntegrationUnits=16, dataRead=5 * 1024 * 1024)]),
        ({"pipelineName": "p_copy", "status": "Failed"}, [_activity("Copy", copy_billing, usedDataIntegrationUnits=16, dataRead=1024)]),
        ({"pipelineName": "zz_mcp_debug_p_meta_abc123", "status": "Succeeded"}, [_activity("GetMetadata", meta_billing), _activity("SetVariable")]),
    ]
    result = compute(pairs, dict(FALLBACK))
    copy_cost = 0.5 * 0.25 + 1 / 1000
    assert result["pipelines"]["p_copy"]["cost"] == pytest.approx(2 * copy_cost)
    assert result["pipelines"]["p_copy"]["failed_cost"] == pytest.approx(copy_cost)
    assert "p_meta (debug runs)" in result["pipelines"]
    assert result["meters"]["Cloud Orchestration Activity Run"]["quantity"] == 4
    total = sum(p["cost"] for p in result["pipelines"].values())
    rules = {r["rule"] for r in recommendations(result, total)}
    assert {"failed_runs", "copy_diu_oversized"} <= rules
