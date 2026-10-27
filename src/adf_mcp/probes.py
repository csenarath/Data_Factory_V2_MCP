"""Connection-test probes for linked services.

ADF's "Test connection" button has no public API, so a probe is a one-activity pipeline that touches the
linked service as lightly as possible: `SELECT 1` for databases, GetMetadata(exists) for storage, an
authenticated GET for Key Vault / Databricks / REST. Unknown types fall back to GetMetadata(exists) on a
dataset that already uses the linked service.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import AdfMcpError

PROBE_POLICY = {"timeout": "0.00:05:00", "retry": 0, "secureInput": False, "secureOutput": False}

# linked service type -> (dataset type, Lookup source type, query property, query)
SQL_PROBES = {
    "AzureSqlDatabase": ("AzureSqlTable", "AzureSqlSource", "sqlReaderQuery", "SELECT 1 AS ok"),
    "AzureSqlMI": ("AzureSqlMITable", "SqlMISource", "sqlReaderQuery", "SELECT 1 AS ok"),
    "AzureSqlDW": ("AzureSqlDWTable", "SqlDWSource", "sqlReaderQuery", "SELECT 1 AS ok"),
    "SqlServer": ("SqlServerTable", "SqlServerSource", "sqlReaderQuery", "SELECT 1 AS ok"),
    "AzurePostgreSql": ("AzurePostgreSqlTable", "AzurePostgreSqlSource", "query", "SELECT 1 AS ok"),
    "PostgreSqlV2": ("PostgreSqlV2Table", "PostgreSqlV2Source", "query", "SELECT 1 AS ok"),
    "AzureMySql": ("AzureMySqlTable", "AzureMySqlSource", "query", "SELECT 1 AS ok"),
    "MySql": ("MySqlTable", "MySqlSource", "query", "SELECT 1 AS ok"),
    "Oracle": ("OracleTable", "OracleSource", "oracleReaderQuery", "SELECT 1 AS ok FROM DUAL"),
    "Snowflake": ("SnowflakeTable", "SnowflakeSource", "query", "SELECT 1 AS ok"),
    "SnowflakeV2": ("SnowflakeV2Table", "SnowflakeV2Source", "query", "SELECT 1 AS ok"),
    "AzureDatabricksDeltaLake": ("AzureDatabricksDeltaLakeDataset", "AzureDatabricksDeltaLakeSource", "query", "SELECT 1 AS ok"),
}

# linked service type -> (location type, property holding the first path segment, or None)
STORAGE_PROBES = {
    "AzureBlobFS": ("AzureBlobFSLocation", "fileSystem"),
    "AzureBlobStorage": ("AzureBlobStorageLocation", "container"),
    "AzureDataLakeStore": ("AzureDataLakeStoreLocation", None),
    "AzureFileStorage": ("AzureFileStorageLocation", None),
    "FileServer": ("FileServerLocation", None),
    "Sftp": ("SftpLocation", None),
    "FtpServer": ("FtpServerLocation", None),
    "AmazonS3": ("AmazonS3Location", "bucketName"),
    "GoogleCloudStorage": ("GoogleCloudStorageLocation", "bucketName"),
}

DATABRICKS_RESOURCE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"  # Azure Databricks first-party app id


@dataclass
class Probe:
    description: str
    pipeline: dict
    dataset: dict | None = None  # temporary dataset (named PROBE_DATASET) the pipeline reads
    existing_dataset: str | None = None  # or an existing dataset the probe goes through
    notes: list[str] = field(default_factory=list)


PROBE_DATASET = "probe_dataset"


def _ls_ref(name: str, parameters: dict | None) -> dict:
    ref = {"referenceName": name, "type": "LinkedServiceReference"}
    if parameters:
        ref["parameters"] = parameters
    return ref


def _activity(name: str, type_: str, type_properties: dict, **extra) -> dict:
    return {"name": name, "type": type_, "policy": PROBE_POLICY, "typeProperties": type_properties, **extra}


def build_probe(
    name: str,
    definition: dict,
    path: str | None = None,
    linked_service_parameters: dict | None = None,
    datasets_using: list[str] | None = None,
) -> Probe:
    ls_type = definition.get("type")
    tp = definition.get("typeProperties") or {}
    ds_ref = {"referenceName": PROBE_DATASET, "type": "DatasetReference"}

    if ls_type in SQL_PROBES:
        ds_type, source_type, query_prop, query = SQL_PROBES[ls_type]
        return Probe(
            f"Lookup `{query}` through {ls_type}",
            {"activities": [_activity("probe", "Lookup", {"source": {"type": source_type, query_prop: query}, "dataset": ds_ref, "firstRowOnly": True})]},
            dataset={"type": ds_type, "linkedServiceName": _ls_ref(name, linked_service_parameters), "typeProperties": {}},
        )

    if ls_type in STORAGE_PROBES:
        location_type, root_key = STORAGE_PROBES[ls_type]
        parts = [p for p in (path or "").replace("\\", "/").split("/") if p]
        location: dict = {"type": location_type}
        if root_key:
            if not parts:
                if datasets_using:
                    return _via_dataset(datasets_using[0], ls_type)
                raise AdfMcpError(
                    "bad_argument",
                    f"{ls_type} needs a path to probe, starting with the {root_key} (e.g. path='raw' or 'raw/folder').",
                )
            location[root_key] = parts.pop(0)
        if parts:
            location["folderPath"] = "/".join(parts)
        return Probe(
            f"GetMetadata(exists) on {path or '/'} via {ls_type}",
            {"activities": [_activity("probe", "GetMetadata", {"dataset": ds_ref, "fieldList": ["exists"]})]},
            dataset={"type": "Binary", "linkedServiceName": _ls_ref(name, linked_service_parameters), "typeProperties": {"location": location}},
            notes=["exists=false still proves connectivity and authentication; it only means the path is missing."],
        )

    if ls_type == "AzureKeyVault":
        base = str(tp.get("baseUrl") or "").rstrip("/")
        if not base or base.startswith("@"):
            raise AdfMcpError("bad_argument", "The Key Vault linked service has no static baseUrl to probe.")
        return Probe(
            "List secrets (1) with the factory's managed identity",
            {"activities": [_activity("probe", "WebActivity", {
                "url": f"{base}/secrets?api-version=7.4&maxresults=1",
                "method": "GET",
                "authentication": {"type": "MSI", "resource": "https://vault.azure.net"},
            })]},
            notes=["Tests the factory managed identity's Key Vault access (needs Key Vault Secrets User or list permission) from the Azure IR."],
        )

    if ls_type == "AzureDatabricks":
        domain = str(tp.get("domain") or "").rstrip("/")
        if tp.get("authentication") != "MSI" or not domain or domain.startswith("@"):
            raise AdfMcpError(
                "unsupported_probe",
                "Only managed-identity (authentication=MSI) Databricks linked services with a static domain can be probed.",
                "For access-token linked services, debug-run a tiny notebook activity instead.",
            )
        headers = {}
        if tp.get("workspaceResourceId"):
            headers["X-Databricks-Azure-Workspace-Resource-Id"] = tp["workspaceResourceId"]
        return Probe(
            "List clusters with the factory's managed identity",
            {"activities": [_activity("probe", "WebActivity", {
                "url": f"{domain}/api/2.0/clusters/list",
                "method": "GET",
                "headers": headers,
                "authentication": {"type": "MSI", "resource": DATABRICKS_RESOURCE},
            })]},
            notes=["Calls the Databricks API from the Azure IR; private-link-only workspaces need the IR the linked service uses."],
        )

    if ls_type == "RestService":
        return Probe(
            f"GET {path or '/'} through the REST linked service",
            {"activities": [_activity("probe", "Lookup", {
                "source": {"type": "RestSource", "requestMethod": "GET", "httpRequestTimeout": "00:01:40"},
                "dataset": ds_ref,
                "firstRowOnly": True,
            })]},
            dataset={"type": "RestResource", "linkedServiceName": _ls_ref(name, linked_service_parameters), "typeProperties": {"relativeUrl": path or ""}},
        )

    if datasets_using:
        return _via_dataset(datasets_using[0], ls_type)
    raise AdfMcpError(
        "unsupported_probe",
        f"No built-in probe for {ls_type} and no dataset uses this linked service.",
        "Create a dataset on it, or debug-run a small pipeline that reads from it.",
    )


def _via_dataset(dataset: str, ls_type: str | None) -> Probe:
    return Probe(
        f"GetMetadata(exists) through existing dataset {dataset}",
        {"activities": [_activity("probe", "GetMetadata", {"dataset": {"referenceName": dataset, "type": "DatasetReference"}, "fieldList": ["exists"]})]},
        existing_dataset=dataset,
        notes=[f"No direct probe used for {ls_type}; went through dataset {dataset}. Dataset parameters without defaults will fail."],
    )
