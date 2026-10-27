"""Toolset `cost`: ADF spend from billed units in activity runs, priced with Azure retail list prices.

Every activity run reports what ADF bills for it in output.billingReference (DIU-hours for copies, execution
hours for pipeline/external activities, vCore-hours for data flows); each activity run is also one
orchestration run (priced per 1000). Summing those per pipeline gives the cost without Cost Management access.
"""

from __future__ import annotations

from collections import defaultdict

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..compact import time_window
from ..errors import handle_errors
from ..jobs import run_job
from ..pricing import ir_prefix, meter_for, price_table
from ..resources import KINDS
from ..stores import LiveStore
from ..tempdeploy import DEBUG_PREFIX, original_name
from ._common import ctx, query_activity_runs, query_pipeline_runs

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
MB = 1024 * 1024


def _new_bucket() -> dict:
    return {"cost": 0.0, "runs": 0, "failed_runs": 0, "failed_cost": 0.0, "activity_runs": 0, "meters": defaultdict(float)}


def compute(runs_with_activities: list[tuple[dict, list[dict]]], prices: dict[str, float]) -> dict:
    """Aggregate cost per pipeline, meter and activity type; collect facts for recommendations."""
    pipelines: dict[str, dict] = defaultdict(_new_bucket)
    meters: dict[str, dict] = defaultdict(lambda: {"quantity": 0.0, "cost": 0.0})
    by_activity_type: dict[str, float] = defaultdict(float)
    copies: list[dict] = []
    unpriced: set[str] = set()

    for run, activities in runs_with_activities:
        name = run.get("pipelineName") or "?"
        if name.startswith(DEBUG_PREFIX):
            name = f"{original_name(name)} (debug runs)"
        bucket = pipelines[name]
        bucket["runs"] += 1
        run_cost = 0.0
        for activity in activities:
            output = activity.get("output") if isinstance(activity.get("output"), dict) else {}
            billing = output.get("billingReference") or {}
            entries = billing.get("billableDuration") or []
            prefix = ir_prefix(entries[0].get("meterType") if entries else None, output.get("effectiveIntegrationRuntime"))
            orchestration = f"{prefix} Orchestration Activity Run"
            cost = prices.get(orchestration, 1.0) / 1000
            meters[orchestration]["quantity"] += 1
            meters[orchestration]["cost"] += cost
            bucket["meters"][orchestration] += cost
            for entry in entries:
                meter = meter_for(billing.get("activityType"), entry.get("meterType"))
                quantity = float(entry.get("duration") or 0)
                if not meter or meter not in prices:
                    unpriced.add(f"{billing.get('activityType')}/{entry.get('meterType')}")
                    continue
                amount = quantity * prices[meter]
                meters[meter]["quantity"] += quantity
                meters[meter]["cost"] += amount
                bucket["meters"][meter] += amount
                cost += amount
            by_activity_type[activity.get("activityType") or "?"] += cost
            run_cost += cost
            bucket["activity_runs"] += 1
            if activity.get("activityType") == "Copy" and output:
                copies.append({
                    "pipeline": name,
                    "activity": activity.get("activityName"),
                    "diu": output.get("usedDataIntegrationUnits"),
                    "data_read_mb": round((output.get("dataRead") or 0) / MB, 2),
                    "cost": round(cost, 4),
                })
        bucket["cost"] += run_cost
        if run.get("status") == "Failed":
            bucket["failed_runs"] += 1
            bucket["failed_cost"] += run_cost
    return {"pipelines": pipelines, "meters": meters, "by_activity_type": by_activity_type, "copies": copies, "unpriced": sorted(unpriced)}


def recommendations(result: dict, total: float) -> list[dict]:
    recs = []
    failed = sum(p["failed_cost"] for p in result["pipelines"].values())
    if total and failed / total > 0.1:
        worst = max(result["pipelines"].items(), key=lambda kv: kv[1]["failed_cost"])
        recs.append({
            "rule": "failed_runs",
            "finding": f"{failed / total:.0%} of spend (${failed:.2f}) went to runs that failed; most in {worst[0]}.",
            "action": "Fix the recurring failure (adf_diagnose_failure) and use rerun-from-failure instead of full reruns.",
            "potential_saving": round(failed, 2),
        })
    for name, p in result["pipelines"].items():
        orchestration = sum(v for k, v in p["meters"].items() if "Orchestration" in k)
        if p["runs"] >= 20 and p["cost"] and orchestration / p["cost"] > 0.6:
            recs.append({
                "rule": "orchestration_heavy",
                "finding": f"{name}: {p['runs']} runs, {orchestration / p['cost']:.0%} of its cost is per-activity orchestration.",
                "action": "Run less often or batch work (fewer, larger runs; fewer tiny ForEach iterations / control activities).",
                "potential_saving": round(orchestration * 0.5, 2),
            })
    for copy in result["copies"]:
        if (copy["diu"] or 0) >= 8 and copy["data_read_mb"] < 100:
            recs.append({
                "rule": "copy_diu_oversized",
                "finding": f"{copy['pipeline']}/{copy['activity']}: {copy['diu']} DIUs for {copy['data_read_mb']} MB.",
                "action": "Set dataIntegrationUnits to 2-4 for small copies (Auto often picks more than needed).",
                "potential_saving": round(copy["cost"] * 0.5, 4),
            })
    flows = {k: v for k, v in result["meters"].items() if k.startswith("Data Flow")}
    if flows and sum(v["cost"] for v in flows.values()) > 0.25 * total:
        recs.append({
            "rule": "data_flow_compute",
            "finding": f"Data flows are ${sum(v['cost'] for v in flows.values()):.2f} of ${total:.2f}.",
            "action": "Use a TTL on the data flow IR so sequential flows reuse a warm cluster, right-size core count, and try Compute Optimized for simple transforms.",
            "potential_saving": None,
        })
    seen, unique = set(), []
    for rec in sorted(recs, key=lambda r: -(r["potential_saving"] or 0)):
        key = (rec["rule"], rec["finding"].split(":")[0])
        if key not in seen:
            seen.add(key)
            unique.append(rec)
    return unique[:10]


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_cost_report(
        hours_back: float = 168,
        pipeline: str | None = None,
        max_runs: int = 300,
        top: int = 15,
        wait_seconds: int = 45,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Estimate ADF spend over a window (default 7 days): per pipeline, per meter (data movement DIU-hours,
        pipeline/external activity hours, data flow vCore-hours, orchestration) and per activity type, with the
        share wasted on failed runs, a 30-day projection, and optimization recommendations with estimated savings.
        Uses billed units reported by ADF itself and Azure retail list prices for the factory's region.
        Scans up to max_runs pipeline runs (newest first); runs as a background job."""
        session, target, client = ctx(factory_name, resource_group)
        region = session.factory(target).get("location", "")

        def job(j):
            prices, price_source = price_table(region)
            after, before = time_window(hours_back, None, None)
            filters = [{"operand": "PipelineName", "operator": "Equals", "values": [pipeline]}] if pipeline else []
            body = {"lastUpdatedAfter": after, "lastUpdatedBefore": before, "filters": filters, "orderBy": [{"orderBy": "RunStart", "order": "DESC"}]}
            runs, truncated = query_pipeline_runs(client, target, body, max_runs)
            j.log(f"{len(runs)} pipeline runs in window; reading activity runs")
            pairs = []
            for i, run in enumerate(runs, 1):
                pairs.append((run, query_activity_runs(client, target, run, limit=1000)[0]))
                if i % 25 == 0:
                    j.log(f"read {i}/{len(runs)} runs")
            result = compute(pairs, prices)
            total = sum(p["cost"] for p in result["pipelines"].values())
            hours = max(1e-9, hours_back)
            ranked = sorted(result["pipelines"].items(), key=lambda kv: -kv[1]["cost"])
            report = {
                "factory": target.factory_name,
                "window": {"from": after, "to": before, "hours": hours_back},
                "runs_scanned": len(runs),
                "scan_truncated": truncated,
                "currency": "USD",
                "price_source": price_source,
                "total_cost": round(total, 4),
                "projected_30_days": round(total * 720 / hours, 2),
                "failed_runs_cost": round(sum(p["failed_cost"] for p in result["pipelines"].values()), 4),
                "by_meter": {k: {"quantity": round(v["quantity"], 4), "cost": round(v["cost"], 4)} for k, v in sorted(result["meters"].items(), key=lambda kv: -kv[1]["cost"])},
                "by_activity_type": {k: round(v, 4) for k, v in sorted(result["by_activity_type"].items(), key=lambda kv: -kv[1])},
                "top_pipelines": [
                    {"pipeline": n, "cost": round(p["cost"], 4), "runs": p["runs"], "failed_runs": p["failed_runs"],
                     "cost_per_run": round(p["cost"] / p["runs"], 5) if p["runs"] else None, "activity_runs": p["activity_runs"]}
                    for n, p in ranked[:top]
                ],
                "recommendations": recommendations(result, total),
                "notes": [
                    "Pay-as-you-go list prices; agreements, reservations and currency change the real bill (see Cost Management).",
                    "Excludes read/write and monitoring operations, inactive-pipeline fees and SSIS IR / Airflow hours.",
                ],
            }
            if result["unpriced"]:
                report["unpriced_meters"] = result["unpriced"]
            if hours_back >= 720 and not pipeline:
                live = LiveStore(session, target)
                ran = {original_name(n) for n in result["pipelines"]}
                inactive = [n for n in live.list(KINDS["pipeline"]) if n not in ran and not n.startswith(DEBUG_PREFIX)]
                if inactive:
                    report["inactive_pipelines"] = {"count": len(inactive), "monthly_fee": round(len(inactive) * prices.get("Inactive Pipeline", 0.8), 2), "pipelines": inactive[:20]}
            return report

        return run_job(session.jobs, "cost", f"cost report {hours_back}h", job, wait_seconds)
