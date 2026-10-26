"""Toolset `authoring`: read and change definitions (any resource kind) and drive the factory's Git repo.

Changes go to Git (a feature branch in the local clone) for Git-connected factories and to the live
factory otherwise; see guards.authoring_mode. Every change is a dry run unless dry_run=false.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..checks import fmt, linked_service_issues, pipeline_issues, trigger_issues
from ..compact import truncate
from ..errors import AdfMcpError, handle_errors
from ..gitrepo import GitRepo, pull_request_url
from ..guards import audit, authoring_mode, check_write, git_repo_configuration, require_confirm_token
from ..guards import confirm_token as make_confirm_token
from ..resources import BUILTIN, KINDS, Kind, check_name, kind_of, normalize, references, same_definition
from ..session import Session, Target
from ..stores import GitStore, LiveStore, Source, git_repo, resolve_store
from ..templates import template
from ._common import ctx

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

PUBLISH_NOTE = (
    "Committed to a feature branch only. Runs keep using the published definition until the branch is merged "
    "and published from ADF Studio / CI. Use adf_pipeline_debug_run to test the branch version now."
)


# -- planning -----------------------------------------------------------------------


def _branch_for(repo: GitRepo, prefix: str, branch: str | None, kind: Kind, name: str) -> str:
    if branch:
        return branch
    current = repo.current_branch()
    if current != repo.collaboration_branch:
        return current
    return prefix + re.sub(r"[^a-z0-9._-]+", "-", f"{kind.name}-{name}".lower()).strip("-")


def _plan(session: Session, target: Target, kind: Kind, name: str, branch: str | None):
    """Where a change to (kind, name) would be written: (mode, store holding the current state, branch, label)."""
    if authoring_mode(session, target) == "git":
        repo = git_repo(session, target)
        branch = _branch_for(repo, target.profile.git.branch_prefix, branch, kind, name)
        if repo.current_branch() == branch:
            ref = None
        else:
            ref = branch if repo.ref_exists(branch) else repo.base_ref()
        return "git", GitStore(repo, ref), branch, f"git branch {branch}"
    return "live", LiveStore(session, target), None, f"live factory {target.factory_name}"


def _diff(old: Any, new: Any, name: str) -> str:
    before = json.dumps(normalize(old), indent=2).splitlines() if old is not None else []
    after = json.dumps(normalize(new), indent=2).splitlines() if new is not None else []
    text = "\n".join(difflib.unified_diff(before, after, f"current/{name}", f"proposed/{name}", lineterm="", n=2))
    return truncate(text, 8000) or ""


def _issues(store, kind: Kind, name: str, definition: dict) -> list[dict]:
    if kind.name == "pipeline":
        return pipeline_issues(name, definition)
    if kind.name == "linkedService":
        return linked_service_issues(name, definition)
    if kind.name == "trigger":
        pipelines = {ref: store.get(KINDS["pipeline"], ref) for k, ref in references(definition) if k == "pipeline"}
        return trigger_issues(name, definition, {k: v for k, v in pipelines.items() if v is not None})
    return []


def _unwrap(definition: dict) -> dict:
    """Accept either the properties object or a whole {"name", "properties"} document."""
    if isinstance(definition, dict) and isinstance(definition.get("properties"), dict) and set(definition) <= {"name", "properties", "type", "etag", "id"}:
        return definition["properties"]
    return definition


def upsert(
    session: Session,
    target: Target,
    kind: Kind,
    name: str,
    definition: dict,
    *,
    dry_run: bool,
    branch: str | None,
    commit_message: str | None,
    force: bool,
) -> dict:
    check_name(name)
    if not isinstance(definition, dict) or not definition:
        raise AdfMcpError("bad_argument", "properties must be a non-empty JSON object.")
    mode, store, branch, where = _plan(session, target, kind, name, branch)
    current = store.get(kind, name)
    if kind.name == "trigger" and mode == "live":
        # Live runtimeState is runtime, not definition (LiveStore preserves it across updates).
        definition = {k: v for k, v in definition.items() if k != "runtimeState"}
        current = {k: v for k, v in current.items() if k != "runtimeState"} if current is not None else None
    missing = sorted(
        fmt(ref)
        for ref in references(definition)
        if ref not in BUILTIN and ref != (kind.name, name) and store.get(KINDS[ref[0]], ref[1]) is None
    )
    issues = _issues(store, kind, name, definition)
    action = "create" if current is None else ("no_change" if same_definition(current, definition) else "update")
    result: dict = {
        "mode": mode,
        "target": where,
        "resource": f"{kind.name}:{name}",
        "action": action,
        "dry_run": dry_run,
        "diff": _diff(current, definition, name) if action != "no_change" else "",
    }
    if missing:
        result["missing_references"] = missing
    if issues:
        result["issues"] = issues
    blocking = bool(missing) or any(i["severity"] == "error" for i in issues)
    if dry_run or action == "no_change":
        if action != "no_change":
            result["next"] = (
                "Fix the missing references / errors, or pass force=true. " if blocking else ""
            ) + "Call again with dry_run=false to apply."
        return result
    if blocking and not force:
        raise AdfMcpError(
            "validation_failed",
            f"{kind.name} '{name}' has missing references {missing} or errors; nothing was written.",
            "Create the referenced resources first, fix the errors, or pass force=true.",
        )
    check_write(target, f"{action} {kind.name}")
    if mode == "git":
        repo = store.repo  # type: ignore[union-attr]
        created = repo.ensure_branch(branch)  # type: ignore[arg-type]
        path = repo.write(kind, name, definition)
        commit = repo.commit([path], commit_message or f"adf-mcp: {action} {kind.name} {name}")
        result.update(branch=branch, branch_created=created, file=path, commit=commit, note=PUBLISH_NOTE)
    else:
        store.put(kind, name, definition)  # type: ignore[union-attr]
    audit(target, f"{kind.name}_{action}", f"{kind.name}/{name}", {"mode": mode, "branch": branch, "commit": result.get("commit")})
    return result


def _summary(kind: Kind, name: str, d: dict) -> dict:
    out: dict = {"name": name}
    folder = (d.get("folder") or {}).get("name")
    if kind.name == "pipeline":
        out.update(activities=len(d.get("activities") or []), parameters=sorted(d.get("parameters") or {}) or None)
    elif kind.name == "dataset":
        out.update(type=d.get("type"), linked_service=(d.get("linkedServiceName") or {}).get("referenceName"))
    elif kind.name == "linkedService":
        out.update(type=d.get("type"), connect_via=(d.get("connectVia") or {}).get("referenceName"))
    elif kind.name == "trigger":
        pipelines = [p.get("pipelineReference", {}).get("referenceName") for p in d.get("pipelines") or []]
        if d.get("pipeline"):
            pipelines.append(d["pipeline"].get("pipelineReference", {}).get("referenceName"))
        out.update(type=d.get("type"), runtime_state=d.get("runtimeState"), pipelines=pipelines)
    else:
        out.update(type=d.get("type"))
    if folder:
        out["folder"] = folder
    return {k: v for k, v in out.items() if v is not None}


def _repo(session: Session, target: Target) -> GitRepo:
    if authoring_mode(session, target) != "git":
        raise AdfMcpError("not_git_mode", f"Factory '{target.factory_name}' is not authored through Git.")
    return git_repo(session, target)


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_resource_list(
        kind: str,
        source: Source = "auto",
        ref: str | None = None,
        name_contains: str | None = None,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """List resources of one kind with a short summary each.

        kind: pipeline, dataset, linkedService, trigger, dataflow, integrationRuntime, credential.
        source: auto (Git working tree for Git-connected factories, else live) | live | git; ref reads a Git branch/commit.
        """
        session, target, _ = ctx(factory_name, resource_group)
        resolved = resolve_store(session, target, source, ref)
        k = kind_of(kind)
        items = resolved.store.list_definitions(k)
        rows = [_summary(k, n, d) for n, d in sorted(items.items()) if not name_contains or name_contains.lower() in n.lower()]
        out = {"source": resolved.store.label, "kind": k.name, "count": len(rows), "items": rows}
        if resolved.note:
            out["note"] = resolved.note
        return out

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_resource_get(
        kind: str,
        name: str,
        source: Source = "auto",
        ref: str | None = None,
        max_chars: int = 40000,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Get one resource's full definition (its `properties`). Secrets read from live are masked by ADF."""
        session, target, _ = ctx(factory_name, resource_group)
        resolved = resolve_store(session, target, source, ref)
        k = kind_of(kind)
        definition = resolved.store.get(k, name)
        if definition is None:
            raise AdfMcpError("not_found", f"{k.name} '{name}' not found in {resolved.store.label}.", f"Call adf_resource_list kind={k.name}.")
        text = json.dumps(definition)
        out = {"source": resolved.store.label, "resource": f"{k.name}:{name}"}
        if len(text) > max_chars:
            out["properties_truncated"] = truncate(text, max_chars)
        else:
            out["properties"] = definition
        out["references"] = sorted(fmt(r) for r in references(definition))
        return out

    @mcp.tool(annotations=WRITE)
    @handle_errors
    def adf_resource_upsert(
        kind: str,
        name: str,
        properties: dict,
        dry_run: bool = True,
        branch: str | None = None,
        commit_message: str | None = None,
        force: bool = False,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Create or update a pipeline, dataset, linkedService, trigger, dataflow, integrationRuntime or credential.

        properties is the resource's `properties` object (see adf_resource_template). dry_run=true (default)
        returns a diff, missing references and validation issues without writing. Git-connected factories:
        commits to `branch` (default: the current feature branch, or adf-mcp/<kind>-<name>), never to the
        collaboration branch. Other factories: writes live (triggers are stopped and restarted around updates).
        """
        session, target, _ = ctx(factory_name, resource_group)
        return upsert(
            session, target, kind_of(kind), name, _unwrap(properties),
            dry_run=dry_run, branch=branch, commit_message=commit_message, force=force,
        )

    @mcp.tool(annotations=WRITE)
    @handle_errors
    def adf_pipeline_edit(
        pipeline: str,
        upsert_activities: list[dict] | None = None,
        remove_activities: list[str] | None = None,
        set_parameters: dict | None = None,
        remove_parameters: list[str] | None = None,
        set_variables: dict | None = None,
        description: str | None = None,
        dry_run: bool = True,
        branch: str | None = None,
        commit_message: str | None = None,
        force: bool = False,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Edit part of a pipeline without resending all of it: add or replace top-level activities (matched by name),
        remove activities (dependsOn links to them are dropped too), and set/remove parameters and variables.
        Same dry-run, validation and Git behaviour as adf_resource_upsert."""
        session, target, _ = ctx(factory_name, resource_group)
        kind = KINDS["pipeline"]
        _, store, resolved_branch, _ = _plan(session, target, kind, pipeline, branch)
        current = store.get(kind, pipeline)
        if current is None:
            raise AdfMcpError("not_found", f"pipeline '{pipeline}' not found in {store.label}.")
        new = copy.deepcopy(current)
        activities = new.setdefault("activities", [])
        for name in remove_activities or []:
            if not any(a.get("name") == name for a in activities):
                raise AdfMcpError("not_found", f"Top-level activity '{name}' not found in pipeline '{pipeline}'.")
            activities[:] = [a for a in activities if a.get("name") != name]
            for a in activities:
                if a.get("dependsOn"):
                    a["dependsOn"] = [d for d in a["dependsOn"] if d.get("activity") != name]
        for activity in upsert_activities or []:
            if not isinstance(activity, dict) or not activity.get("name") or not activity.get("type"):
                raise AdfMcpError("bad_argument", "Each activity needs at least 'name' and 'type'.")
            index = next((i for i, a in enumerate(activities) if a.get("name") == activity["name"]), None)
            if index is None:
                activities.append(activity)
            else:
                activities[index] = activity
        if set_parameters:
            new.setdefault("parameters", {}).update(set_parameters)
        for name in remove_parameters or []:
            (new.get("parameters") or {}).pop(name, None)
        if set_variables:
            new.setdefault("variables", {}).update(set_variables)
        if description is not None:
            new["description"] = description
        return upsert(
            session, target, kind, pipeline, new,
            dry_run=dry_run, branch=resolved_branch, commit_message=commit_message, force=force,
        )

    @mcp.tool(annotations=DELETE)
    @handle_errors
    def adf_resource_delete(
        kind: str,
        name: str,
        dry_run: bool = True,
        confirm_token: str | None = None,
        branch: str | None = None,
        commit_message: str | None = None,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Delete a resource. dry_run=true (default) lists what references it and returns a confirm_token;
        the delete needs dry_run=false plus that token. Git mode removes the file on a feature branch."""
        session, target, _ = ctx(factory_name, resource_group)
        k = kind_of(kind)
        mode, store, resolved_branch, where = _plan(session, target, k, name, branch)
        if store.get(k, name) is None:
            raise AdfMcpError("not_found", f"{k.name} '{name}' not found in {store.label}.")
        referenced_by = []
        for other_kind in KINDS.values():
            for other, definition in store.list_definitions(other_kind).items():
                if (k.name, name) in references(definition):
                    referenced_by.append(f"{other_kind.name}:{other}")
        token_parts = ("delete", k.name, name, mode, resolved_branch)
        if dry_run:
            return {
                "mode": mode,
                "target": where,
                "resource": f"{k.name}:{name}",
                "referenced_by": referenced_by,
                "warning": "Deleting breaks the resources listed in referenced_by." if referenced_by else None,
                "confirm_token": make_confirm_token(target, *token_parts),
                "next": "Show this to the user; to delete call again with dry_run=false and confirm_token.",
            }
        require_confirm_token(target, confirm_token, *token_parts)
        check_write(target, f"delete {k.name}")
        result: dict = {"mode": mode, "target": where, "resource": f"{k.name}:{name}", "deleted": True}
        if mode == "git":
            repo = store.repo  # type: ignore[union-attr]
            created = repo.ensure_branch(resolved_branch)  # type: ignore[arg-type]
            path = repo.remove(k, name)
            commit = repo.commit([path], commit_message or f"adf-mcp: delete {k.name} {name}")
            result.update(branch=resolved_branch, branch_created=created, commit=commit, note=PUBLISH_NOTE)
        else:
            store.delete(k, name)  # type: ignore[union-attr]
        audit(target, f"{k.name}_delete", f"{k.name}/{name}", {"mode": mode, "branch": resolved_branch})
        return result

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    @handle_errors
    def adf_resource_template(kind: str, type: str | None = None) -> dict:
        """Starter `properties` for common resource types (placeholders in <ANGLE_BRACKETS>, secrets via Key Vault).
        Omit `type` to list the templates available for a kind."""
        return template(kind_of(kind).name, type)

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_git_status(factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Git-connected factories: the clone's branch, uncommitted files, commits ahead/behind the collaboration
        branch, ADF resources changed on this branch, and the pull-request link."""
        session, target, _ = ctx(factory_name, resource_group)
        repo = _repo(session, target)
        branch, base = repo.current_branch(), repo.base_ref()
        ahead, behind = repo.ahead_behind(branch, base)
        out: dict = {
            "repo_path": str(repo.path),
            "branch": branch,
            "collaboration_branch": repo.collaboration_branch,
            "compared_to": base,
            "ahead": ahead,
            "behind": behind,
            "uncommitted": repo.dirty_files(),
            "changed_resources": repo.changed_resources(branch, base) if branch != repo.collaboration_branch else [],
        }
        if branch != repo.collaboration_branch:
            out["pull_request_url"] = pull_request_url(git_repo_configuration(session.factory(target)) or {}, branch)
        return out

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    @handle_errors
    def adf_git_checkout(
        branch: str, create: bool = False, factory_name: str | None = None, resource_group: str | None = None
    ) -> dict:
        """Switch the clone to a branch (create=true makes a new feature branch from the collaboration branch).
        Refuses when the clone has uncommitted changes."""
        session, target, _ = ctx(factory_name, resource_group)
        repo = _repo(session, target)
        if create:
            created = repo.ensure_branch(branch)
        else:
            if repo.dirty_files():
                raise AdfMcpError("git_dirty", "The clone has uncommitted changes; not switching.", "Commit or stash them first.")
            if not repo.ref_exists(branch) and repo.ref_exists(f"origin/{branch}"):
                repo.git("switch", "--track", f"origin/{branch}")
            else:
                repo.git("switch", branch)
            created = False
        return {"branch": repo.current_branch(), "created": created}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    @handle_errors
    def adf_git_sync(factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Fetch from origin and fast-forward the collaboration branch if it is checked out."""
        session, target, _ = ctx(factory_name, resource_group)
        repo = _repo(session, target)
        repo.fetch()
        branch = repo.current_branch()
        updated = False
        if branch == repo.collaboration_branch and not repo.dirty_files():
            before = repo.git("rev-parse", "HEAD").strip()
            repo.git("merge", "--ff-only", f"origin/{branch}")
            updated = before != repo.git("rev-parse", "HEAD").strip()
        ahead, behind = repo.ahead_behind(branch, repo.base_ref())
        return {"branch": branch, "fast_forwarded": updated, "ahead": ahead, "behind": behind}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    @handle_errors
    def adf_git_push(branch: str | None = None, factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Push a feature branch to origin and return the link to open a pull request into the collaboration branch."""
        session, target, _ = ctx(factory_name, resource_group)
        repo = _repo(session, target)
        branch = branch or repo.current_branch()
        if branch == repo.collaboration_branch:
            raise AdfMcpError("git_error", "Refusing to push the collaboration branch; push a feature branch and open a PR.")
        check_write(target, "git push")
        repo.push(branch)
        audit(target, "git_push", f"branch/{branch}")
        return {
            "pushed": branch,
            "pull_request_url": pull_request_url(git_repo_configuration(session.factory(target)) or {}, branch),
            "changed_resources": repo.changed_resources(branch, repo.base_ref()),
        }

