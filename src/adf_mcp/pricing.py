"""ADF list prices from the public Azure Retail Prices API (no auth), cached for 24h per region.

Prices are pay-as-you-go list prices in USD; agreements, reservations and currency change real bills.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from .config import home_dir

API = "https://prices.azure.com/api/retail/prices"
CACHE_SECONDS = 24 * 3600

# Used when the API is unreachable (USD list prices, 2026).
FALLBACK = {
    "Cloud Data Movement": 0.25,
    "Cloud Pipeline Activity": 0.005,
    "Cloud External Pipeline Activity": 0.00025,
    "Cloud Orchestration Activity Run": 1.0,
    "Self Hosted Data Movement": 0.10,
    "Self Hosted Pipeline Activity": 0.002,
    "Self Hosted External Pipeline Activity": 0.0001,
    "Self Hosted Orchestration Activity Run": 1.5,
    "Azure Managed VNET Data Movement": 0.25,
    "Azure Managed VNET Pipeline Activity": 1.0,
    "Azure Managed VNET External Pipeline Activity": 1.0,
    "Azure Managed VNET Orchestration Activity Run": 1.0,
    "Data Flow General Purpose": 0.274,
    "Data Flow Compute Optimized": 0.199,
    "Data Flow Memory Optimized": 0.343,
    "Inactive Pipeline": 0.80,
}

# billingReference meterType -> meter name prefix
IR_PREFIX = {"azureir": "Cloud", "selfhostedir": "Self Hosted", "managedvnetir": "Azure Managed VNET"}
# billingReference activityType -> meter name suffix
CATEGORY = {"datamovement": "Data Movement", "pipelineactivity": "Pipeline Activity", "externalactivity": "External Pipeline Activity"}
DATA_FLOW = {"general": "Data Flow General Purpose", "computeoptimized": "Data Flow Compute Optimized", "memoryoptimized": "Data Flow Memory Optimized"}


def _fetch(region: str) -> dict[str, float]:
    query = f"serviceName eq 'Azure Data Factory v2' and armRegionName eq '{region}' and priceType eq 'Consumption'"
    url = f"{API}?$filter={urllib.parse.quote(query)}"
    prices: dict[str, float] = {}
    while url:
        with urllib.request.urlopen(url, timeout=30) as response:
            page = json.load(response)
        for item in page.get("Items", []):
            product, meter, price = item.get("productName", ""), item.get("meterName", ""), item.get("retailPrice")
            if product == "Azure Data Factory v2" and meter and price is not None and meter != "Operations":
                prices[meter] = price
            elif product.startswith("Azure Data Factory v2 Data Flow - ") and meter == "vCore":
                prices["Data Flow " + product.removeprefix("Azure Data Factory v2 Data Flow - ")] = price
        url = page.get("NextPageLink")
    return prices


def price_table(region: str) -> tuple[dict[str, float], str]:
    """(meter name -> USD price, source description). Orchestration prices are per 1000 activity runs."""
    region = (region or "eastus").replace(" ", "").lower()
    cache = home_dir() / f"prices_{region}.json"
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_SECONDS:
            return {**FALLBACK, **json.loads(cache.read_text())}, f"Azure Retail Prices ({region}, cached)"
        prices = _fetch(region)
        if prices:
            cache.write_text(json.dumps(prices))
            return {**FALLBACK, **prices}, f"Azure Retail Prices ({region})"
    except Exception:  # noqa: BLE001 - fall back to list prices
        pass
    return dict(FALLBACK), "built-in USD list prices (Retail Prices API unreachable)"


def meter_for(activity_type: str | None, meter_type: str | None) -> str | None:
    """Meter name for one billableDuration entry of an activity's billingReference."""
    kind = (activity_type or "").replace("_", "").lower()
    meter = (meter_type or "").replace("_", "").replace(" ", "").lower()
    if kind == "executedataflow":
        return DATA_FLOW.get(meter, "Data Flow General Purpose")
    prefix, category = IR_PREFIX.get(meter), CATEGORY.get(kind)
    return f"{prefix} {category}" if prefix and category else None


def ir_prefix(meter_type: str | None, effective_ir: str | None) -> str:
    """IR family for orchestration pricing, from the billing meter or the IR name the activity ran on."""
    prefix = IR_PREFIX.get((meter_type or "").lower())
    if prefix:
        return prefix
    if effective_ir and "managed" in effective_ir.lower() and "vnet" in effective_ir.lower():
        return "Azure Managed VNET"
    if effective_ir and effective_ir.split(" ")[0] not in ("", "AutoResolveIntegrationRuntime") and "(" not in effective_ir:
        return "Self Hosted"
    return "Cloud"
