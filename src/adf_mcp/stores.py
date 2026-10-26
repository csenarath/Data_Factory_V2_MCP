"""One interface over the two places definitions live: the live factory and its Git repo."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from azure.core.exceptions import ResourceNotFoundError

from .errors import AdfMcpError
from .gitrepo import GitRepo, clone_url
from .guards import authoring_mode, git_repo_configuration
from .resources import KINDS, Kind
from .session import Session, Target

Source = Literal["auto", "live", "git"]


class Store(Protocol):
    label: str

    def list(self, kind: Kind) -> list[str]: ...
    def get(self, kind: Kind, name: str) -> dict | None: ...


class LiveStore:
    def __init__(self, session: Session, target: Target):
        self.client = session.adf(target)
        self.target = target
        self.label = "live"

    def _ops(self, kind: Kind):
        return getattr(self.client, kind.ops)

    def list(self, kind: Kind) -> list[str]:
        items = self._ops(kind).list_by_factory(self.target.resource_group, self.target.factory_name)
        return sorted(item.as_dict()["name"] for item in items)

    def list_definitions(self, kind: Kind) -> dict[str, dict]:
        items = self._ops(kind).list_by_factory(self.target.resource_group, self.target.factory_name)
        return {d["name"]: d.get("properties") or {} for d in (item.as_dict() for item in items)}

    def get(self, kind: Kind, name: str) -> dict | None:
        try:
            item = self._ops(kind).get(self.target.resource_group, self.target.factory_name, name)
        except ResourceNotFoundError:
            return None
        return (item.as_dict().get("properties") or {}) if item is not None else None

    def put(self, kind: Kind, name: str, definition: dict) -> None:
        if kind.name == "trigger":
            self._put_trigger(name, definition)
            return
        self._ops(kind).create_or_update(self.target.resource_group, self.target.factory_name, name, {"properties": definition})

    def _put_trigger(self, name: str, definition: dict) -> None:
        # A started trigger can't be updated: stop, update, then restore its runtime state.
        rg, factory = self.target.resource_group, self.target.factory_name
        current = self.get(KINDS["trigger"], name)
        was_started = bool(current) and current.get("runtimeState") == "Started"
        if was_started:
            self.client.triggers.begin_stop(rg, factory, name).result()
        body = {k: v for k, v in definition.items() if k != "runtimeState"}
        self.client.triggers.create_or_update(rg, factory, name, {"properties": body})
        if was_started:
            self.client.triggers.begin_start(rg, factory, name).result()

    def delete(self, kind: Kind, name: str) -> None:
        rg, factory = self.target.resource_group, self.target.factory_name
        if kind.name == "trigger":
            current = self.get(kind, name)
            if current and current.get("runtimeState") == "Started":
                self.client.triggers.begin_stop(rg, factory, name).result()
        self._ops(kind).delete(rg, factory, name)


class GitStore:
    def __init__(self, repo: GitRepo, ref: str | None = None):
        self.repo = repo
        self.ref = ref
        self.label = f"git:{ref}" if ref else f"git:{repo.current_branch()} (working tree)"

    def list(self, kind: Kind) -> list[str]:
        return self.repo.list(kind, self.ref)

    def list_definitions(self, kind: Kind) -> dict[str, dict]:
        return {name: self.repo.read(kind, name, self.ref) or {} for name in self.list(kind)}

    def get(self, kind: Kind, name: str) -> dict | None:
        return self.repo.read(kind, name, self.ref)


def git_repo(session: Session, target: Target) -> GitRepo:
    """The local clone for a Git-connected factory, validated against its repo configuration."""
    repo_config = git_repo_configuration(session.factory(target))
    if not repo_config:
        raise AdfMcpError("not_git_connected", f"Factory '{target.factory_name}' has no Git configuration.")
    settings = target.profile.git
    if not settings.repo_path:
        raise AdfMcpError(
            "git_repo_not_configured",
            f"Factory '{target.factory_name}' is Git-connected but profile '{target.profile.name}' has no git.repo_path.",
            f"Clone {clone_url(repo_config)} and set git.repo_path in ~/.adf-mcp/profiles.yaml.",
        )
    key = ("git", settings.repo_path)
    cached = session.cache.get(key)
    if cached is None:
        cached = GitRepo(
            settings.repo_path,
            root_folder=settings.root_folder or repo_config.get("rootFolder") or "/",
            collaboration_branch=repo_config.get("collaborationBranch") or "main",
        )
        remote = (cached.remote_url() or "").lower()
        if repo_config.get("repositoryName", "").lower() not in remote:
            raise AdfMcpError(
                "git_repo_mismatch",
                f"{settings.repo_path} (origin {remote or 'none'}) is not the factory's repo {repo_config.get('repositoryName')}.",
            )
        session.cache[key] = cached
    return cached


@dataclass
class Resolved:
    store: LiveStore | GitStore
    mode: Literal["live", "git"]
    note: str | None = None


def resolve_store(session: Session, target: Target, source: Source = "auto", ref: str | None = None) -> Resolved:
    """Pick where to read definitions. auto = Git working tree for Git-connected factories, else live."""
    if source == "live":
        return Resolved(LiveStore(session, target), "live")
    if source == "git" or ref:
        return Resolved(GitStore(git_repo(session, target), ref), "git")
    if authoring_mode(session, target) == "git":
        try:
            return Resolved(GitStore(git_repo(session, target)), "git")
        except AdfMcpError as exc:
            if exc.code != "git_repo_not_configured":
                raise
            return Resolved(LiveStore(session, target), "live", note=f"Read from live: {exc.message} {exc.hint}")
    return Resolved(LiveStore(session, target), "live")
