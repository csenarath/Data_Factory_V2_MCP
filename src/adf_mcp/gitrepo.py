"""The factory's Git repository (a local clone), driven through the git CLI.

Reads can target any ref (``git show ref:path``) without touching the working tree. Writes
always happen in the working tree, on a feature branch, and are committed immediately.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .errors import AdfMcpError
from .resources import KINDS, Kind, git_document

GIT_TIMEOUT = 120


class GitRepo:
    def __init__(self, path: str | Path, root_folder: str = "/", collaboration_branch: str = "main"):
        self.path = Path(path).expanduser()
        self.root = root_folder.strip("/")
        self.collaboration_branch = collaboration_branch
        if not (self.path / ".git").exists():
            raise AdfMcpError("git_error", f"{self.path} is not a Git clone.", "Set git.repo_path in the profile to a clone of the factory repo.")

    # -- plumbing -------------------------------------------------------------------

    def git(self, *args: str, check: bool = True) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GIT_TIMEOUT,
            env=env,
        )
        if check and proc.returncode != 0:
            raise AdfMcpError("git_error", f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[:500]}")
        return proc.stdout

    def rel(self, kind: Kind, name: str | None = None) -> str:
        parts = [p for p in (self.root, kind.name, f"{name}.json" if name else None) if p]
        return "/".join(parts)

    # -- state ----------------------------------------------------------------------

    def current_branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def ref_exists(self, ref: str) -> bool:
        return self.git("rev-parse", "--verify", "--quiet", ref, check=False).strip() != ""

    def base_ref(self) -> str:
        """The collaboration branch, preferring the remote-tracking ref."""
        remote = f"origin/{self.collaboration_branch}"
        return remote if self.ref_exists(remote) else self.collaboration_branch

    def dirty_files(self) -> list[str]:
        return [line[3:] for line in self.git("status", "--porcelain").splitlines() if line.strip()]

    def remote_url(self) -> str | None:
        url = self.git("remote", "get-url", "origin", check=False).strip()
        return url or None

    def ahead_behind(self, branch: str, base: str) -> tuple[int, int]:
        out = self.git("rev-list", "--left-right", "--count", f"{base}...{branch}", check=False).split()
        return (int(out[1]), int(out[0])) if len(out) == 2 else (0, 0)

    def changed_resources(self, branch: str, base: str) -> list[dict]:
        """ADF resources changed on `branch` since it forked from `base`."""
        changes = []
        for line in self.git("diff", "--name-status", f"{base}...{branch}").splitlines():
            status, _, path = line.partition("\t")
            parsed = self.parse_path(path.split("\t")[-1])
            if parsed:
                changes.append({"change": {"A": "added", "M": "modified", "D": "deleted"}.get(status[:1], status), "kind": parsed[0], "name": parsed[1]})
        return changes

    def parse_path(self, path: str) -> tuple[str, str] | None:
        prefix = f"{self.root}/" if self.root else ""
        if not path.startswith(prefix) or not path.endswith(".json"):
            return None
        folder, _, file = path[len(prefix) :].partition("/")
        if folder in KINDS and "/" not in file:
            return folder, file[: -len(".json")]
        return None

    # -- reads (ref=None reads the working tree) --------------------------------------

    def list(self, kind: Kind, ref: str | None = None) -> list[str]:
        if ref is None:
            folder = self.path / self.rel(kind)
            return sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []
        out = self.git("ls-tree", "--name-only", f"{ref}:{self.rel(kind)}", check=False)
        return sorted(Path(n).stem for n in out.splitlines() if n.endswith(".json"))

    def read(self, kind: Kind, name: str, ref: str | None = None) -> dict | None:
        if ref is None:
            file = self.path / self.rel(kind, name)
            if not file.exists():
                return None
            text = file.read_text(encoding="utf-8-sig")
        else:
            proc = subprocess.run(
                ["git", "-C", str(self.path), "show", f"{ref}:{self.rel(kind, name)}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=GIT_TIMEOUT,
            )
            if proc.returncode != 0:
                return None
            text = proc.stdout.lstrip("﻿")
        try:
            return json.loads(text).get("properties") or {}
        except json.JSONDecodeError as exc:
            raise AdfMcpError("git_error", f"{self.rel(kind, name)} at {ref or 'working tree'} is not valid JSON: {exc}") from exc

    # -- writes ---------------------------------------------------------------------

    def ensure_branch(self, branch: str) -> bool:
        """Check out `branch`, creating it from the collaboration branch if needed. Returns True if created."""
        if branch == self.collaboration_branch:
            raise AdfMcpError(
                "git_error",
                f"Refusing to commit to the collaboration branch '{branch}'.",
                "Changes go to a feature branch and reach the collaboration branch through a pull request.",
            )
        if self.current_branch() == branch:
            return False
        dirty = self.dirty_files()
        if dirty:
            raise AdfMcpError(
                "git_dirty",
                f"The clone has uncommitted changes ({', '.join(dirty[:5])}); not switching branches.",
                "Commit or stash them first.",
            )
        if self.ref_exists(branch):
            self.git("switch", branch)
            return False
        self.git("switch", "-c", branch, self.base_ref())
        return True

    def write(self, kind: Kind, name: str, definition: dict) -> str:
        rel = self.rel(kind, name)
        file = self.path / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(git_document(kind, name, definition), encoding="utf-8", newline="\n")
        return rel

    def remove(self, kind: Kind, name: str) -> str:
        rel = self.rel(kind, name)
        self.git("rm", "--quiet", rel)
        return rel

    def commit(self, paths: list[str], message: str) -> str | None:
        existing = [p for p in paths if (self.path / p).exists()]  # removals are already staged by `git rm`
        if existing:
            self.git("add", "--", *existing)
        if not self.git("diff", "--cached", "--name-only").strip():
            return None  # nothing changed
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "--short", "HEAD").strip()

    def fetch(self) -> None:
        self.git("fetch", "--prune", "origin")

    def push(self, branch: str) -> str:
        return self.git("push", "--set-upstream", "origin", branch)


def pull_request_url(repo_configuration: dict, branch: str) -> str | None:
    """Browser URL to open a PR from `branch` into the collaboration branch."""
    target = repo_configuration.get("collaborationBranch", "main")
    account, repo = repo_configuration.get("accountName"), repo_configuration.get("repositoryName")
    if repo_configuration.get("type") == "FactoryVSTSConfiguration":
        project = repo_configuration.get("projectName")
        return (
            f"https://dev.azure.com/{account}/{project}/_git/{repo}/pullrequestcreate"
            f"?sourceRef={branch}&targetRef={target}"
        )
    host = repo_configuration.get("hostName") or "https://github.com"
    return f"{host.rstrip('/')}/{account}/{repo}/compare/{target}...{branch}?expand=1"


def clone_url(repo_configuration: dict) -> str | None:
    account, repo = repo_configuration.get("accountName"), repo_configuration.get("repositoryName")
    if repo_configuration.get("type") == "FactoryVSTSConfiguration":
        return f"https://dev.azure.com/{account}/{repo_configuration.get('projectName')}/_git/{repo}"
    host = repo_configuration.get("hostName") or "https://github.com"
    return f"{host.rstrip('/')}/{account}/{repo}.git"
