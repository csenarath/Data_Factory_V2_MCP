"""Temporary deployments: publish `zz_mcp_debug_*` copies of definitions, run a pipeline, delete the copies.

Used by debug runs (unpublished Git versions) and linked-service connection tests (probe pipelines).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from .compact import pipeline_run_view
from .guards import audit
from .jobs import Job
from .resources import KINDS, REFERENCE_TYPES, references, same_definition
from .session import Session, Target
from .stores import GitStore, LiveStore

DEBUG_PREFIX = "zz_mcp_debug_"
DEBUG_FOLDER = "_mcp_debug"
COPYABLE = ("pipeline", "dataflow", "dataset", "linkedService")  # IRs and credentials must already be published
TERMINAL = {"Succeeded", "Failed", "Cancelled"}

Ref = tuple[str, str]


def original_name(name: str) -> str:
    """zz_mcp_debug_<name>_<hex6> -> <name>; other names unchanged."""
    return name[len(DEBUG_PREFIX) :].rsplit("_", 1)[0] if name.startswith(DEBUG_PREFIX) else name


def debug_copies(git: GitStore, live: LiveStore, root: Ref) -> tuple[list[Ref], list[str]]:
    """Resources that need a temporary published copy for `root` to behave as it does in Git.

    A resource needs a copy if its Git version is missing in or differs from live, or if anything it
    references needs a copy (so the copy can point at the other copies). The root is always copied.
    Returns them dependencies-first, plus references that can't be satisfied (missing in both Git and
    live, or non-copyable kinds missing live).
    """
    need: dict[Ref, bool] = {}
    order: list[Ref] = []
    unresolved: list[str] = []

    def visit(ref: Ref, stack: frozenset) -> bool:
        if ref in need:
            return need[ref]
        if ref in stack:  # reference cycle: treat as not needing a copy
            return False
        kind, name = ref
        if kind not in COPYABLE:
            if ref != ("integrationRuntime", "AutoResolveIntegrationRuntime") and live.get(KINDS[kind], name) is None:
                unresolved.append(f"{kind}:{name}")
            need[ref] = False
            return False
        git_def = git.get(KINDS[kind], name)
        if git_def is None:
            if live.get(KINDS[kind], name) is None:
                unresolved.append(f"{kind}:{name}")
            need[ref] = False
            return False
        children = [visit(child, stack | {ref}) for child in sorted(references(git_def))]
        changed = ref == root or not same_definition(git_def, live.get(KINDS[kind], name))
        need[ref] = changed or any(children)
        if need[ref]:
            order.append(ref)
        return need[ref]

    visit(root, frozenset())
    return order, unresolved


def rewrite(definition, renames: dict[Ref, str]):
    """Point references at temporary copies."""
    if isinstance(definition, dict):
        out = {k: rewrite(v, renames) for k, v in definition.items()}
        kind = REFERENCE_TYPES.get(definition.get("type")) if isinstance(definition.get("type"), str) else None
        if kind and (kind, definition.get("referenceName")) in renames:
            out["referenceName"] = renames[(kind, definition["referenceName"])]
        return out
    if isinstance(definition, list):
        return [rewrite(v, renames) for v in definition]
    return definition


@dataclass
class TempPlan:
    root: Ref  # the pipeline to run
    copies: list[Ref]  # dependencies first
    definitions: dict[Ref, dict]  # as in the source; references are rewritten at deploy time
    suffix: str = field(default_factory=lambda: secrets.token_hex(3))

    @property
    def renames(self) -> dict[Ref, str]:
        return {ref: f"{DEBUG_PREFIX}{ref[1]}"[:130] + f"_{self.suffix}" for ref in self.copies}


def execute(job: Job, session: Session, target: Target, plan: TempPlan, parameters: dict | None, wait_seconds: float, keep: bool, label: str) -> dict:
    """Job body: deploy the copies, run the root, wait (up to an hour), delete the copies."""
    from .tools._common import failed_activities, wait_for_run

    live, client = LiveStore(session, target), session.adf(target)
    renames, created = plan.renames, []
    try:
        for ref in plan.copies:  # dependencies first
            body = rewrite(plan.definitions[ref], renames)
            if ref[0] in ("pipeline", "dataflow", "dataset"):
                body = {**body, "folder": {"name": DEBUG_FOLDER}}
            live.put(KINDS[ref[0]], renames[ref], body)
            created.append((ref[0], renames[ref]))
            job.log(f"published temporary {ref[0]} {renames[ref]}")
        run_id = client.pipelines.create_run(target.resource_group, target.factory_name, renames[plan.root], parameters or None).as_dict()["runId"]
    except Exception:
        for kind, name in reversed(created):
            live.delete(KINDS[kind], name)
        job.log("rolled back temporary copies")
        raise
    job.log(f"run started: {run_id}")
    audit(target, label, f"{plan.root[0]}/{plan.root[1]}", {"temp_copies": [f"{k}:{n}" for k, n in created], "run_id": run_id})

    run = wait_for_run(client, target, run_id, min(wait_seconds, 3600))
    view = pipeline_run_view(run, message_chars=1000)
    if run.get("status") == "Failed":
        view["failed_activities"] = failed_activities(client, target, run)
    out = {"run_id": run_id, "temp_copies": [f"{k}:{n}" for k, n in created], "run": view}
    if run.get("status") in TERMINAL and not keep:
        for kind, name in reversed(created):
            live.delete(KINDS[kind], name)
        out["temp_copies_deleted"] = True
        job.log("temporary copies deleted")
    else:
        out["cleanup"] = "Temporary copies kept; call adf_debug_cleanup when the run has finished."
    return out
