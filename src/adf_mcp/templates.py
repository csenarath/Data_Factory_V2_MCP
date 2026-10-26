"""Starter definitions (the `properties` object) for common resource types.

Placeholders are written as <ANGLE_BRACKETS>. Secrets are always Key Vault references.
"""

from __future__ import annotations

import copy

_KV_SECRET = {"type": "AzureKeyVaultSecret", "store": {"referenceName": "<KEY_VAULT_LINKED_SERVICE>", "type": "LinkedServiceReference"}, "secretName": "<SECRET_NAME>"}

TEMPLATES: dict[str, dict[str, dict]] = {
    "linkedService": {
        "AzureKeyVault": {"type": "AzureKeyVault", "typeProperties": {"baseUrl": "https://<VAULT_NAME>.vault.azure.net/"}},
        "AzureBlobFS": {
            "type": "AzureBlobFS",
            "description": "ADLS Gen2 via the factory's managed identity",
            "typeProperties": {"url": "https://<STORAGE_ACCOUNT>.dfs.core.windows.net/"},
        },
        "AzureBlobStorage": {
            "type": "AzureBlobStorage",
            "typeProperties": {"serviceEndpoint": "https://<STORAGE_ACCOUNT>.blob.core.windows.net/", "accountKind": "StorageV2"},
        },
        "AzureSqlDatabase": {
            "type": "AzureSqlDatabase",
            "typeProperties": {
                "server": "<SERVER>.database.windows.net",
                "database": "<DATABASE>",
                "encrypt": "mandatory",
                "trustServerCertificate": False,
                "authenticationType": "SystemAssignedManagedIdentity",
            },
        },
        "AzureDatabricks": {
            "type": "AzureDatabricks",
            "typeProperties": {
                "domain": "https://<WORKSPACE>.azuredatabricks.net",
                "authentication": "MSI",
                "workspaceResourceId": "/subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.Databricks/workspaces/<WORKSPACE>",
                "existingClusterId": "<CLUSTER_ID>",
            },
        },
        "SqlServer": {
            "type": "SqlServer",
            "connectVia": {"referenceName": "<SELF_HOSTED_IR>", "type": "IntegrationRuntimeReference"},
            "typeProperties": {
                "server": "<HOST>",
                "database": "<DATABASE>",
                "authenticationType": "SQL",
                "userName": "<USER>",
                "password": _KV_SECRET,
                "encrypt": "mandatory",
            },
        },
        "RestService": {
            "type": "RestService",
            "typeProperties": {"url": "https://<API_HOST>/", "authenticationType": "Anonymous", "enableServerCertificateValidation": True},
        },
    },
    "dataset": {
        "Parquet": {
            "type": "Parquet",
            "linkedServiceName": {"referenceName": "<ADLS_LINKED_SERVICE>", "type": "LinkedServiceReference"},
            "parameters": {"folder": {"type": "string"}},
            "typeProperties": {
                "location": {"type": "AzureBlobFSLocation", "fileSystem": "<CONTAINER>", "folderPath": "@dataset().folder"},
                "compressionCodec": "snappy",
            },
        },
        "DelimitedText": {
            "type": "DelimitedText",
            "linkedServiceName": {"referenceName": "<ADLS_LINKED_SERVICE>", "type": "LinkedServiceReference"},
            "typeProperties": {
                "location": {"type": "AzureBlobFSLocation", "fileSystem": "<CONTAINER>", "folderPath": "<FOLDER>", "fileName": "<FILE>.csv"},
                "columnDelimiter": ",",
                "escapeChar": "\\",
                "firstRowAsHeader": True,
                "quoteChar": '"',
            },
        },
        "AzureSqlTable": {
            "type": "AzureSqlTable",
            "linkedServiceName": {"referenceName": "<SQL_LINKED_SERVICE>", "type": "LinkedServiceReference"},
            "typeProperties": {"schema": "<SCHEMA>", "table": "<TABLE>"},
        },
    },
    "pipeline": {
        "Copy": {
            "activities": [
                {
                    "name": "copy_data",
                    "type": "Copy",
                    "policy": {"timeout": "0.02:00:00", "retry": 2, "retryIntervalInSeconds": 60},
                    "inputs": [{"referenceName": "<SOURCE_DATASET>", "type": "DatasetReference"}],
                    "outputs": [{"referenceName": "<SINK_DATASET>", "type": "DatasetReference"}],
                    "typeProperties": {
                        "source": {"type": "<SOURCE_TYPE e.g. AzureSqlSource>"},
                        "sink": {"type": "<SINK_TYPE e.g. ParquetSink>"},
                        "enableStaging": False,
                    },
                }
            ],
            "annotations": [],
        },
        "DatabricksNotebook": {
            "parameters": {"run_date": {"type": "string", "defaultValue": "@utcNow('yyyy-MM-dd')"}},
            "activities": [
                {
                    "name": "run_notebook",
                    "type": "DatabricksNotebook",
                    "policy": {"timeout": "0.04:00:00", "retry": 1, "retryIntervalInSeconds": 120},
                    "linkedServiceName": {"referenceName": "<DATABRICKS_LINKED_SERVICE>", "type": "LinkedServiceReference"},
                    "typeProperties": {"notebookPath": "/<PATH>", "baseParameters": {"run_date": "@pipeline().parameters.run_date"}},
                }
            ],
        },
        "ForEachExecutePipeline": {
            "parameters": {"items": {"type": "array"}},
            "activities": [
                {
                    "name": "for_each_item",
                    "type": "ForEach",
                    "typeProperties": {
                        "items": "@pipeline().parameters.items",
                        "isSequential": False,
                        "batchCount": 4,
                        "activities": [
                            {
                                "name": "process_item",
                                "type": "ExecutePipeline",
                                "typeProperties": {
                                    "pipeline": {"referenceName": "<CHILD_PIPELINE>", "type": "PipelineReference"},
                                    "waitOnCompletion": True,
                                    "parameters": {"item": "@item()"},
                                },
                            }
                        ],
                    },
                }
            ],
        },
    },
    "trigger": {
        "ScheduleTrigger": {
            "type": "ScheduleTrigger",
            "runtimeState": "Stopped",
            "pipelines": [{"pipelineReference": {"referenceName": "<PIPELINE>", "type": "PipelineReference"}, "parameters": {}}],
            "typeProperties": {
                "recurrence": {
                    "frequency": "Day",
                    "interval": 1,
                    "startTime": "<YYYY-MM-DDT00:00:00>",
                    "timeZone": "Sri Lanka Standard Time",
                    "schedule": {"hours": [2], "minutes": [0]},
                }
            },
        },
        "TumblingWindowTrigger": {
            "type": "TumblingWindowTrigger",
            "runtimeState": "Stopped",
            "pipeline": {
                "pipelineReference": {"referenceName": "<PIPELINE>", "type": "PipelineReference"},
                "parameters": {"window_start": "@trigger().outputs.windowStartTime", "window_end": "@trigger().outputs.windowEndTime"},
            },
            "typeProperties": {
                "frequency": "Hour",
                "interval": 1,
                "startTime": "<YYYY-MM-DDT00:00:00Z>",
                "delay": "00:00:00",
                "maxConcurrency": 1,
                "retryPolicy": {"count": 2, "intervalInSeconds": 300},
            },
        },
        "BlobEventsTrigger": {
            "type": "BlobEventsTrigger",
            "runtimeState": "Stopped",
            "pipelines": [
                {
                    "pipelineReference": {"referenceName": "<PIPELINE>", "type": "PipelineReference"},
                    "parameters": {"file_name": "@triggerBody().fileName", "folder_path": "@triggerBody().folderPath"},
                }
            ],
            "typeProperties": {
                "blobPathBeginsWith": "/<CONTAINER>/blobs/<FOLDER>/",
                "blobPathEndsWith": ".csv",
                "ignoreEmptyBlobs": True,
                "scope": "/subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.Storage/storageAccounts/<ACCOUNT>",
                "events": ["Microsoft.Storage.BlobCreated"],
            },
        },
    },
    "dataflow": {
        "MappingDataFlow": {
            "type": "MappingDataFlow",
            "typeProperties": {
                "sources": [{"name": "source1", "dataset": {"referenceName": "<SOURCE_DATASET>", "type": "DatasetReference"}}],
                "sinks": [{"name": "sink1", "dataset": {"referenceName": "<SINK_DATASET>", "type": "DatasetReference"}}],
                "transformations": [],
                "scriptLines": [
                    "source(allowSchemaDrift: true, validateSchema: false) ~> source1",
                    "source1 sink(allowSchemaDrift: true, validateSchema: false) ~> sink1",
                ],
            },
        },
    },
    "integrationRuntime": {
        "SelfHosted": {"type": "SelfHosted", "description": "<DESCRIPTION>"},
        "ManagedAzure": {
            "type": "Managed",
            "typeProperties": {"computeProperties": {"location": "AutoResolve", "dataFlowProperties": {"computeType": "General", "coreCount": 8, "timeToLive": 10}}},
        },
    },
}


def template(kind: str, type_name: str | None) -> dict:
    available = TEMPLATES.get(kind, {})
    if not type_name:
        return {"kind": kind, "available_types": sorted(available)}
    match = next((t for t in available if t.lower() == type_name.lower()), None)
    if not match:
        return {"kind": kind, "error": f"No template for {type_name!r}", "available_types": sorted(available)}
    return {"kind": kind, "type": match, "properties": copy.deepcopy(available[match])}
