"""Static checks over factory definitions: references, unused resources, pipeline and trigger rules."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .resources import BUILTIN, KINDS, iter_activities, references

Ref = tuple[str, str]  # (kind, name)

_PARAM_RE = re.compile(r"pipeline\(\)\.parameters\.([A-Za-z_][A-Za-z0-9_]*)")
_PARAM_BRACKET_RE = re.compile(r"pipeline\(\)\.parameters\[\s*'([^']+)'\s*\]")
_VARIABLE_RE = re.compile(r"variables\(\s*'([^']+)'\s*\)")
_ACTIVITY_RE = re.compile(r"activity\(\s*'([^']+)'\s*\)")


def issue(severity: str, kind: str, name: str, message: str, where: str | None = None) -> dict:
    out = {"severity": severity, "resource": f"{kind}:{name}", "message": message}
    if where:
        out["where"] = where
    return out


def pipeline_issues(name: str, pipeline: dict) -> list[dict]:
    """Rules ADF Studio's validation enforces, plus expression references that would fail at runtime."""
    found: list[dict] = []
    activities = list(iter_activities(pipeline.get("activities")))
    names = [a.get("name") for _, a, _ in activities]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        found.append(issue("error", "pipeline", name, f"Activity name '{dup}' is used more than once."))

    for path, activity, siblings in activities:
        sibling_names = {s.get("name") for s in siblings}
        for dep in activity.get("dependsOn") or []:
            if dep.get("activity") not in sibling_names:
                found.append(issue("error", "pipeline", name, f"dependsOn unknown activity '{dep.get('activity')}'.", path))
        if activity.get("type") == "ExecutePipeline":
            ref = ((activity.get("typeProperties") or {}).get("pipeline") or {}).get("referenceName")
            if ref == name:
                found.append(issue("error", "pipeline", name, "Execute Pipeline calls its own pipeline.", path))
        if activity.get("type") == "SetVariable":
            var = (activity.get("typeProperties") or {}).get("variableName")
            if var and var not in (pipeline.get("variables") or {}) and var != "pipelineReturnValue":
                found.append(issue("error", "pipeline", name, f"Sets undeclared variable '{var}'.", path))

    found.extend(_dependency_cycles(name, pipeline.get("activities") or []))

    text = json.dumps(pipeline.get("activities") or [])
    declared_params = set(pipeline.get("parameters") or {})
    for param in sorted(set(_PARAM_RE.findall(text)) | set(_PARAM_BRACKET_RE.findall(text))):
        if param not in declared_params:
            found.append(issue("error", "pipeline", name, f"Expression uses undeclared parameter '{param}'."))
    declared_vars = set(pipeline.get("variables") or {})
    for var in sorted(set(_VARIABLE_RE.findall(text))):
        if var not in declared_vars:
            found.append(issue("error", "pipeline", name, f"Expression uses undeclared variable '{var}'."))
    for ref in sorted(set(_ACTIVITY_RE.findall(text))):
        if ref not in names:
            found.append(issue("error", "pipeline", name, f"Expression references unknown activity '{ref}'."))
    return found


def _dependency_cycles(name: str, activities: list[dict]) -> list[dict]:
    graph = {a.get("name"): [d.get("activity") for d in a.get("dependsOn") or []] for a in activities}
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        state[node] = 1
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1 or (nxt in graph and state.get(nxt) is None and visit(nxt)):
                return True
        state[node] = 2
        return False

    for node in graph:
        if state.get(node) is None and visit(node):
            return [issue("error", "pipeline", name, f"Activity dependencies form a cycle (involving '{node}').")]
    return []


_ZONED_TIME_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


def trigger_issues(name: str, trigger: dict, pipelines: dict[str, dict]) -> list[dict]:
    found = []
    recurrence = (trigger.get("typeProperties") or {}).get("recurrence") or {}
    start, zone = recurrence.get("startTime"), recurrence.get("timeZone")
    if isinstance(start, str) and zone:
        # ADF rejects starting a trigger whose startTime format doesn't match its timeZone.
        if zone == "UTC" and not start.endswith("Z"):
            found.append(issue("error", "trigger", name, f"timeZone UTC needs startTime ending in 'Z' (got {start})."))
        elif zone != "UTC" and _ZONED_TIME_RE.search(start):
            found.append(issue("error", "trigger", name, f"timeZone {zone} needs a startTime without 'Z'/offset (got {start})."))
    for entry in trigger.get("pipelines") or ([trigger["pipeline"]] if trigger.get("pipeline") else []):
        ref = (entry.get("pipelineReference") or {}).get("referenceName")
        pipeline = pipelines.get(ref)
        if pipeline is None:
            continue  # reported as a missing reference
        supplied = set(entry.get("parameters") or {})
        for param, spec in (pipeline.get("parameters") or {}).items():
            if "defaultValue" not in (spec or {}) and param not in supplied:
                found.append(issue("error", "trigger", name, f"Does not supply required parameter '{param}' of pipeline '{ref}'."))
    return found


def linked_service_issues(name: str, linked_service: dict) -> list[dict]:
    text = json.dumps(linked_service.get("typeProperties") or {})
    if '"SecureString"' in text:
        return [issue("warning", "linkedService", name, "Holds an inline SecureString secret; use an Azure Key Vault reference.")]
    return []


@dataclass
class FactoryModel:
    """All definitions of a factory from one source, with the reference graph between them."""

    definitions: dict[str, dict[str, dict]]  # kind -> name -> definition
    edges: set[tuple[Ref, Ref]] = field(default_factory=set)

    @classmethod
    def load(cls, store) -> FactoryModel:
        definitions = {}
        for kind in KINDS.values():
            definitions[kind.name] = store.list_definitions(kind)
        model = cls(definitions)
        for kind, items in definitions.items():
            for name, definition in items.items():
                for ref in references(definition):
                    model.edges.add(((kind, name), ref))
        return model

    def exists(self, ref: Ref) -> bool:
        return ref in BUILTIN or ref[1] in self.definitions.get(ref[0], {})

    def outgoing(self, ref: Ref) -> list[Ref]:
        return sorted(dst for src, dst in self.edges if src == ref)

    def incoming(self, ref: Ref) -> list[Ref]:
        return sorted(src for src, dst in self.edges if dst == ref)

    def subgraph(self, root: Ref, direction: str = "both", depth: int = 5) -> set[tuple[Ref, Ref]]:
        selected: set[tuple[Ref, Ref]] = set()
        for forward in ([True, False] if direction == "both" else [direction == "down"]):
            frontier, seen = [root], {root}
            for _ in range(depth):
                nxt = []
                for node in frontier:
                    for other in self.outgoing(node) if forward else self.incoming(node):
                        selected.add((node, other) if forward else (other, node))
                        if other not in seen:
                            seen.add(other)
                            nxt.append(other)
                frontier = nxt
        return selected

    def issues(self) -> list[dict]:
        found: list[dict] = []
        for (src_kind, src_name), dst in sorted(self.edges):
            if not self.exists(dst):
                found.append(issue("error", src_kind, src_name, f"References missing {dst[0]} '{dst[1]}'."))

        targets = defaultdict(set)
        for src, dst in self.edges:
            targets[dst].add(src)
        for kind in ("dataset", "linkedService", "dataflow", "integrationRuntime", "credential"):
            for name in self.definitions.get(kind, {}):
                if not targets.get((kind, name)):
                    found.append(issue("info", kind, name, "Not referenced by anything."))
        for name in self.definitions.get("pipeline", {}):
            if not targets.get(("pipeline", name)):
                found.append(issue("info", "pipeline", name, "No trigger or Execute Pipeline activity runs this pipeline."))

        pipelines = self.definitions.get("pipeline", {})
        for name, definition in pipelines.items():
            found.extend(pipeline_issues(name, definition))
        for name, definition in self.definitions.get("trigger", {}).items():
            found.extend(trigger_issues(name, definition, pipelines))
        for name, definition in self.definitions.get("linkedService", {}).items():
            found.extend(linked_service_issues(name, definition))
        order = {"error": 0, "warning": 1, "info": 2}
        return sorted(found, key=lambda i: (order[i["severity"]], i["resource"]))


def fmt(ref: Ref) -> str:
    return f"{ref[0]}:{ref[1]}"
