"""Publish: deploy the collaboration branch to the live factory, the way ADF Studio's Publish button does.

ADF Studio's publish is: validate -> generate ARM templates -> deploy them. The deployed result is every
definition in the collaboration branch made live, with triggers stopped around the change and then set to the
runtimeState stored in Git. This module does that directly through the REST API, diff-based: only resources
that differ are deployed, in dependency order. The adf_publish branch (ARM templates for CI) is not written.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .checks import FactoryModel
from .gitrepo import GitRepo
from .jobs import Job
from .resources import KINDS, references, same_definition
from .session import Session, Target
from .stores import GitStore, LiveStore
from .tempdeploy import DEBUG_PREFIX

# Dependencies first. Credentials before linked services (they reference them); IRs before both.
DEPLOY_ORDER = ["integrationRuntime", "credential", "linkedService", "dataset", "dataflow", "pipeline", "trigger"]

Ref = tuple[str, str]


def _topological(kind: str, names: list[str], definitions: dict[str, dict]) -> list[str]:
    """Order names of one kind so that same-kind references (LS -> LS, pipeline -> pipeline, flowlets) come first."""
    ordered, state = [], {}

    def visit(name: str) -> None:
        if state.get(name):
            return
        state[name] = 1
        for ref_kind, ref_name in sorted(references(definitions.get(name) or {})):
            if ref_kind == kind and ref_name in definitions and ref_name != name:
                visit(ref_name)
        ordered.append(name)

    for name in sorted(names):
        visit(name)
    return [n for n in ordered if n in names]


def _strip_runtime(kind: str, definition: dict | None) -> dict | None:
    if definition is None or kind != "trigger":
        return definition
    return {k: v for k, v in definition.items() if k != "runtimeState"}


@dataclass
class PublishPlan:
    ref: str
    commit: str
    deploy: list[dict] = field(default_factory=list)  # {"action": create|update, "kind", "name"}
    delete: list[dict] = field(default_factory=list)
    only_in_live: list[str] = field(default_factory=list)
    unchanged: int = 0
    triggers_stop_first: list[str] = field(default_factory=list)
    triggers_desired: dict[str, str] = field(default_factory=dict)  # name -> Started | Stopped (from Git)
    triggers_live_state: dict[str, str] = field(default_factory=dict)
    global_parameters: dict | None = None  # new value, if it changes
    errors: list[dict] = field(default_factory=list)
    git_definitions: dict[Ref, dict] = field(default_factory=dict)

    def summary(self) -> dict:
        start_after = sorted(n for n, s in self.triggers_desired.items() if s == "Started")
        stop_after = sorted(n for n, s in self.triggers_desired.items() if s != "Started" and self.triggers_live_state.get(n) == "Started")
        return {
            "ref": self.ref,
            "commit": self.commit,
            "changes": len(self.deploy) + len(self.delete) + (1 if self.global_parameters is not None else 0),
            "deploy": [f"{d['action']} {d['kind']}:{d['name']}" for d in self.deploy],
            "delete": [f"delete {d['kind']}:{d['name']}" for d in self.delete],
            "global_parameters": "update" if self.global_parameters is not None else "unchanged",
            "unchanged_resources": self.unchanged,
            "only_in_live_not_deleted": self.only_in_live,
            "triggers": {"stopped_during_publish": self.triggers_stop_first, "started_after": start_after, "stopped_after": stop_after},
            "validation_errors": self.errors,
        }

    def token_parts(self) -> tuple:
        return ("publish", self.commit, tuple(self.summary()["deploy"]), tuple(self.summary()["delete"]), self.global_parameters is not None)


def plan_publish(session: Session, target: Target, repo: GitRepo, ref: str, delete_removed: bool) -> PublishPlan:
    git, live = GitStore(repo, ref), LiveStore(session, target)
    plan = PublishPlan(ref=ref, commit=repo.head(ref))
    plan.errors = [i for i in FactoryModel.load(git).issues() if i["severity"] == "error"]

    for kind_name in DEPLOY_ORDER:
        kind = KINDS[kind_name]
        in_git, in_live = git.list_definitions(kind), live.list_definitions(kind)
        changed = []
        for name, definition in in_git.items():
            plan.git_definitions[(kind_name, name)] = definition
            current = in_live.get(name)
            if current is None:
                changed.append((name, "create"))
            elif not same_definition(_strip_runtime(kind_name, definition), _strip_runtime(kind_name, current)):
                changed.append((name, "update"))
            else:
                plan.unchanged += 1
        actions = dict(changed)
        for name in _topological(kind_name, list(actions), in_git):
            plan.deploy.append({"action": actions[name], "kind": kind_name, "name": name})
        extra = sorted(n for n in set(in_live) - set(in_git) if not n.startswith(DEBUG_PREFIX))
        if kind_name == "integrationRuntime":
            extra = [n for n in extra if n != "AutoResolveIntegrationRuntime"]
        if delete_removed:
            plan.delete.extend({"kind": kind_name, "name": n} for n in extra)
        else:
            plan.only_in_live.extend(f"{kind_name}:{n}" for n in extra)
        if kind_name == "trigger":
            plan.triggers_live_state = {n: (d.get("runtimeState") or "Stopped") for n, d in in_live.items()}
            plan.triggers_desired = {n: (d.get("runtimeState") or "Stopped") for n, d in in_git.items()}

    # Deletes run in reverse dependency order: triggers, pipelines, ..., IRs.
    order = {k: i for i, k in enumerate(DEPLOY_ORDER)}
    plan.delete.sort(key=lambda d: -order[d["kind"]])

    touched = {d["name"] for d in plan.deploy + plan.delete if d["kind"] == "trigger"}
    plan.triggers_stop_first = sorted(n for n in touched if plan.triggers_live_state.get(n) == "Started")

    factory_doc = repo.factory_document(target.factory_name, ref) or {}
    git_globals = (factory_doc.get("properties") or {}).get("globalParameters") or {}
    live_globals = {}
    for item in session.adf(target).global_parameters.list_by_factory(target.resource_group, target.factory_name):
        live_globals = item.as_dict().get("properties") or {}
    if git_globals != live_globals and (git_globals or live_globals):
        plan.global_parameters = git_globals
    return plan


def apply_publish(job: Job, session: Session, target: Target, plan: PublishPlan) -> dict:
    live, client = LiveStore(session, target), session.adf(target)
    rg, factory = target.resource_group, target.factory_name
    stopped, deployed, deleted = [], [], []
    try:
        for name in plan.triggers_stop_first:
            client.triggers.begin_stop(rg, factory, name).result()
            stopped.append(name)
            job.log(f"stopped trigger {name}")
        for item in plan.deploy:
            kind, name = item["kind"], item["name"]
            definition = _strip_runtime(kind, plan.git_definitions[(kind, name)])
            live.put(KINDS[kind], name, definition)
            deployed.append(f"{kind}:{name}")
            job.log(f"{item['action']}d {kind}:{name}")
        if plan.global_parameters is not None:
            client.global_parameters.create_or_update(rg, factory, "default", {"properties": plan.global_parameters})
            job.log("updated global parameters")
        for item in plan.delete:
            live.delete(KINDS[item["kind"]], item["name"])
            deleted.append(f"{item['kind']}:{item['name']}")
            job.log(f"deleted {item['kind']}:{item['name']}")
    except Exception as exc:
        # Leave triggers as they were before a failed publish.
        for name in stopped:
            if plan.triggers_live_state.get(name) == "Started":
                try:
                    client.triggers.begin_start(rg, factory, name).result()
                    job.log(f"restarted trigger {name} after failure")
                except Exception:  # noqa: BLE001 - best effort, reported below
                    job.log(f"could not restart trigger {name}")
        job.log(f"publish stopped after deploying {len(deployed)} and deleting {len(deleted)}: {exc}")
        raise

    started_after, stopped_after = [], []
    deleted_triggers = {d["name"] for d in plan.delete if d["kind"] == "trigger"}
    for name, desired in sorted(plan.triggers_desired.items()):
        if name in deleted_triggers:
            continue
        current = "Stopped" if name in stopped else plan.triggers_live_state.get(name, "Stopped")
        if desired == "Started" and current != "Started":
            client.triggers.begin_start(rg, factory, name).result()
            started_after.append(name)
            job.log(f"started trigger {name}")
        elif desired != "Started" and current == "Started":
            client.triggers.begin_stop(rg, factory, name).result()
            stopped_after.append(name)
            job.log(f"stopped trigger {name}")
    return {
        "published": plan.commit,
        "ref": plan.ref,
        "deployed": deployed,
        "deleted": deleted,
        "global_parameters_updated": plan.global_parameters is not None,
        "triggers_started": started_after,
        "triggers_stopped": stopped_after,
        "note": "Live now matches Git. The adf_publish branch (ARM templates for CI) was not regenerated.",
    }
