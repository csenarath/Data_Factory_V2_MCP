"""Toolset `health`: end-to-end checks — dependency graph, validation, Git/live drift, factory health."""

from __future__ import annotations

from collections import Counter

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..checks import FactoryModel, fmt
from ..compact import time_window
from ..errors import AdfMcpError, handle_errors
from ..guards import authoring_mode
from ..resources import KINDS, kind_of, normalize, same_definition
from ..stores import GitStore, LiveStore, Source, git_repo, resolve_store
from ._common import DEBUG_PREFIX, ctx, query_pipeline_runs

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _drift(git: GitStore, live: LiveStore, kinds) -> dict:
    report = {"only_in_git": [], "only_in_live": [], "different": []}
    for kind in kinds:
        in_git, in_live = git.list_definitions(kind), live.list_definitions(kind)
        for name in sorted(set(in_git) - set(in_live)):
            report["only_in_git"].append(f"{kind.name}:{name}")
        for name in sorted(set(in_live) - set(in_git)):
            report["only_in_live"].append(f"{kind.name}:{name}")
        for name in sorted(set(in_git) & set(in_live)):
            git_def, live_def = dict(in_git[name]), dict(in_live[name])
            if kind.name == "trigger":  # runtime state is not a definition change
                git_def.pop("runtimeState", None)
                live_def.pop("runtimeState", None)
            if not same_definition(git_def, live_def):
                changed = sorted(
                    k for k in set(normalize(git_def)) | set(normalize(live_def))
                    if normalize(git_def).get(k) != normalize(live_def).get(k)
                )
                report["different"].append({"resource": f"{kind.name}:{name}", "changed_keys": changed})
    return report


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_dependency_graph(
        kind: str | None = None,
        name: str | None = None,
        direction: str = "both",
        depth: int = 5,
        source: Source = "auto",
        ref: str | None = None,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Dependency graph: trigger -> pipeline -> (child pipelines, datasets, data flows, linked services)
        -> linked services -> integration runtimes / Key Vault. With kind+name, returns that resource's
        upstream (direction=up: what uses it), downstream (down: what it needs) or both; otherwise the whole factory."""
        session, target, _ = ctx(factory_name, resource_group)
        resolved = resolve_store(session, target, source, ref)
        model = FactoryModel.load(resolved.store)
        if kind and name:
            root = (kind_of(kind).name, name)
            if not model.exists(root):
                raise AdfMcpError("not_found", f"{fmt(root)} not found in {resolved.store.label}.")
            edges = model.subgraph(root, direction, depth)
        else:
            edges = model.edges
        missing = sorted({fmt(dst) for _, dst in edges if not model.exists(dst)})
        return {
            "source": resolved.store.label,
            "root": fmt(root) if kind and name else None,
            "counts": {k: len(v) for k, v in model.definitions.items() if v},
            "edges": sorted(f"{fmt(src)} -> {fmt(dst)}" for src, dst in edges),
            "missing": missing,
        }

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_validate(
        source: Source = "auto",
        ref: str | None = None,
        include_info: bool = True,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Validate the whole factory: missing references, duplicate/unknown activity names and dependsOn,
        dependency cycles, undeclared parameters/variables/activities in expressions, triggers missing required
        pipeline parameters, inline secrets, and unused resources (info). Runs on Git (working tree or ref) or live."""
        session, target, _ = ctx(factory_name, resource_group)
        resolved = resolve_store(session, target, source, ref)
        issues = FactoryModel.load(resolved.store).issues()
        counts = Counter(i["severity"] for i in issues)
        return {
            "source": resolved.store.label,
            "valid": counts.get("error", 0) == 0,
            "counts": dict(counts),
            "issues": [i for i in issues if include_info or i["severity"] != "info"],
        }

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_drift(
        kind: str | None = None, ref: str | None = None, factory_name: str | None = None, resource_group: str | None = None
    ) -> dict:
        """Compare Git with the live (published) factory. Default ref is the collaboration branch: only_in_git /
        different = merged but not yet published; only_in_live = changed live or deleted in Git."""
        session, target, _ = ctx(factory_name, resource_group)
        repo = git_repo(session, target)
        git_ref = ref or repo.base_ref()
        kinds = [kind_of(kind)] if kind else [k for k in KINDS.values() if k.name != "credential"]
        report = _drift(GitStore(repo, git_ref), LiveStore(session, target), kinds)
        report["in_sync"] = not any(report[k] for k in ("only_in_git", "only_in_live", "different"))
        report["git_ref"] = git_ref
        return report

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_factory_health(
        hours_back: float = 24, factory_name: str | None = None, resource_group: str | None = None
    ) -> dict:
        """One-call health report: integration runtime states, trigger states, run outcomes and still-failing
        pipelines in the window, validation errors, and (Git factories) drift between Git and live."""
        session, target, client = ctx(factory_name, resource_group)
        rg, factory = target.resource_group, target.factory_name
        report: dict = {"factory": factory}

        runtimes = []
        for ir in client.integration_runtimes.list_by_factory(rg, factory):
            name = ir.as_dict()["name"]
            status = client.integration_runtimes.get_status(rg, factory, name).as_dict().get("properties") or {}
            runtimes.append({"name": name, "type": status.get("type"), "state": status.get("state")})
        report["integration_runtimes"] = runtimes or "only AutoResolveIntegrationRuntime"

        triggers = [t.as_dict() for t in client.triggers.list_by_factory(rg, factory)]
        report["triggers"] = {
            "total": len(triggers),
            "started": sorted(t["name"] for t in triggers if t["properties"].get("runtimeState") == "Started"),
            "stopped": sorted(t["name"] for t in triggers if t["properties"].get("runtimeState") != "Started"),
        }

        after, before = time_window(hours_back, None, None)
        body = {"lastUpdatedAfter": after, "lastUpdatedBefore": before, "orderBy": [{"orderBy": "RunStart", "order": "DESC"}]}
        runs, truncated = query_pipeline_runs(client, target, body, 500)
        latest: dict[str, str] = {}
        for run in runs:
            latest.setdefault(run.get("pipelineName"), run.get("status"))
        report["runs"] = {
            "window_hours": hours_back,
            "status_counts": dict(Counter(r.get("status") for r in runs)),
            # Temporary debug copies are deleted after their run; their failures are not factory health.
            "still_failing": sorted(p for p, s in latest.items() if s == "Failed" and not p.startswith(DEBUG_PREFIX)),
            "scan_truncated": truncated,
        }

        resolved = resolve_store(session, target, "auto")
        issues = FactoryModel.load(resolved.store).issues()
        errors = [i for i in issues if i["severity"] == "error"]
        report["validation"] = {
            "source": resolved.store.label,
            "counts": dict(Counter(i["severity"] for i in issues)),
            "errors": errors[:10],
        }

        if authoring_mode(session, target) == "git":
            try:
                repo = git_repo(session, target)
                drift = _drift(GitStore(repo, repo.base_ref()), LiveStore(session, target), [k for k in KINDS.values() if k.name != "credential"])
                report["git_drift"] = {k: (len(v) if isinstance(v, list) else v) for k, v in drift.items()}
                report["git_drift"]["unpublished_or_changed"] = drift["only_in_git"] + [d["resource"] for d in drift["different"]]
            except AdfMcpError as exc:
                report["git_drift"] = f"unavailable: {exc.message}"

        problems = []
        if any(r.get("state") not in ("Online", "Started", None) for r in runtimes):
            problems.append("integration runtime not online")
        if report["runs"]["still_failing"]:
            problems.append(f"{len(report['runs']['still_failing'])} pipeline(s) still failing")
        if errors:
            problems.append(f"{len(errors)} validation error(s)")
        report["status"] = "healthy" if not problems else "attention: " + "; ".join(problems)
        return report
