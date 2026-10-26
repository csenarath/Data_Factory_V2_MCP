"""ADF resource kinds, reference extraction and definition normalization.

A "definition" here is the resource's ``properties`` object, the part that is the same in
the REST API and in the factory's Git JSON files (``{"name", "properties", "type"}``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .errors import AdfMcpError


@dataclass(frozen=True)
class Kind:
    name: str  # canonical kind, also the Git folder name
    ops: str  # DataFactoryManagementClient operations attribute
    arm_type: str  # value of "type" in Git JSON files


KINDS: dict[str, Kind] = {
    k.name: k
    for k in (
        Kind("pipeline", "pipelines", "Microsoft.DataFactory/factories/pipelines"),
        Kind("dataset", "datasets", "Microsoft.DataFactory/factories/datasets"),
        Kind("linkedService", "linked_services", "Microsoft.DataFactory/factories/linkedservices"),
        Kind("trigger", "triggers", "Microsoft.DataFactory/factories/triggers"),
        Kind("dataflow", "data_flows", "Microsoft.DataFactory/factories/dataflows"),
        Kind("integrationRuntime", "integration_runtimes", "Microsoft.DataFactory/factories/integrationruntimes"),
        Kind("credential", "credential_operations", "Microsoft.DataFactory/factories/credentials"),
    )
}
KIND_NAMES = ", ".join(KINDS)

_ALIASES = {
    "linked_service": "linkedService",
    "linkedservice": "linkedService",
    "ls": "linkedService",
    "data_flow": "dataflow",
    "integration_runtime": "integrationRuntime",
    "integrationruntime": "integrationRuntime",
    "ir": "integrationRuntime",
}

REFERENCE_TYPES = {
    "PipelineReference": "pipeline",
    "DatasetReference": "dataset",
    "LinkedServiceReference": "linkedService",
    "DataFlowReference": "dataflow",
    "IntegrationRuntimeReference": "integrationRuntime",
    "CredentialReference": "credential",
}

# Exists in every factory without a definition.
BUILTIN = {("integrationRuntime", "AutoResolveIntegrationRuntime")}


def kind_of(name: str) -> Kind:
    key = name.strip()
    key = _ALIASES.get(key.lower(), key)
    for kind in KINDS.values():
        if kind.name.lower() == key.lower() or kind.name.lower() + "s" == key.lower():
            return kind
    raise AdfMcpError("bad_argument", f"Unknown resource kind {name!r}. Use one of: {KIND_NAMES}.")


def references(definition: Any) -> set[tuple[str, str]]:
    """Every (kind, name) a definition points at, found via {"referenceName", "type": "*Reference"}.

    Parameterized references (referenceName is an expression) are skipped: they can't be resolved statically.
    """
    found: set[tuple[str, str]] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref_kind = REFERENCE_TYPES.get(node.get("type")) if isinstance(node.get("type"), str) else None
            name = node.get("referenceName")
            if ref_kind and isinstance(name, str) and not name.startswith("@"):
                found.add((ref_kind, name))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(definition)
    return found


def iter_activities(activities: list[dict] | None, path: str = "") -> Iterator[tuple[str, dict, list[dict]]]:
    """Yield (path, activity, siblings) for every activity, including those nested in containers."""
    for activity in activities or []:
        if not isinstance(activity, dict):
            continue
        here = f"{path}/{activity.get('name')}" if path else str(activity.get("name"))
        yield here, activity, activities  # type: ignore[misc]
        props = activity.get("typeProperties") or {}
        for key in ("activities", "ifTrueActivities", "ifFalseActivities", "defaultActivities"):
            yield from iter_activities(props.get(key), here)
        for case in props.get("cases") or []:
            yield from iter_activities(case.get("activities"), f"{here}[{case.get('value')}]")


def find_activity(pipeline: dict, name: str) -> dict | None:
    for _, activity, _ in iter_activities(pipeline.get("activities")):
        if activity.get("name") == name:
            return activity
    return None


def normalize(value: Any) -> Any:
    """Canonical form for comparing definitions from different sources (Git vs live).

    ADF Studio wraps expressions as {"value": "@...", "type": "Expression"} and adds empty defaults
    (dependsOn: [], userProperties: [], secureInput: false) that the REST API omits. lastPublishTime is
    a system field ADF stamps on publish, not part of the definition.
    """
    if isinstance(value, dict):
        if set(value) == {"value", "type"} and value.get("type") == "Expression":
            return value["value"]
        out = {}
        for key, item in value.items():
            item = normalize(item)
            if item in ([], {}, None) or (key in ("secureInput", "secureOutput") and item is False) or key == "lastPublishTime":
                continue
            out[key] = item
        return out
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


def same_definition(a: Any, b: Any) -> bool:
    return normalize(a) == normalize(b)


def git_document(kind: Kind, name: str, definition: dict) -> str:
    """Serialize exactly like ADF Studio: tab indentation, LF (Git converts), no trailing newline."""
    return json.dumps({"name": name, "properties": definition, "type": kind.arm_type}, indent="\t", ensure_ascii=False)


_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\- .]{0,259}$")


def check_name(name: str) -> str:
    if not _NAME_RE.match(name) or name.endswith((".", " ")):
        raise AdfMcpError("bad_argument", f"Invalid ADF resource name {name!r}.")
    return name
