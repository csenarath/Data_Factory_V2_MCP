"""Write safety: read-only / prod protection, Git-mode routing, confirm tokens and the audit log."""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from typing import Any, Literal

from .config import AuthoringTarget, home_dir
from .errors import AdfMcpError
from .session import Session, Target


def check_write(target: Target, operation: str) -> None:
    """Raise if the profile forbids writes. Applies to runtime and authoring writes alike."""
    reason = target.profile.writes_blocked_reason
    if reason:
        raise AdfMcpError(
            "write_blocked",
            f"{operation} refused: {reason}.",
            "Use a non-prod profile, or set allow_prod_writes / read_only in profiles.yaml deliberately.",
        )


def git_repo_configuration(factory: dict) -> dict | None:
    return (factory.get("properties") or {}).get("repoConfiguration")


def authoring_mode(session: Session, target: Target) -> Literal["git", "live"]:
    """Where definition changes (pipelines, datasets, ...) must go for this factory.

    Live writes to a Git-connected factory are overwritten by the next publish from the
    collaboration branch, so they are refused unless the profile opts in explicitly.
    """
    profile = target.profile
    repo = git_repo_configuration(session.factory(target))
    if profile.authoring_target == AuthoringTarget.GIT:
        return "git"
    if profile.authoring_target == AuthoringTarget.LIVE:
        if repo and not profile.allow_live_writes_on_git_factory:
            raise AdfMcpError(
                "live_write_on_git_factory",
                f"Factory '{target.factory_name}' is Git-connected ({repo.get('repositoryName')}); "
                "live changes would be overwritten on the next publish.",
                "Author in Git mode instead, or set allow_live_writes_on_git_factory: true for this profile.",
            )
        return "live"
    return "git" if repo else "live"


_TOKEN_KEY = secrets.token_bytes(32)  # per process, so tokens never outlive the session


def confirm_token(target: Target, *parts: Any) -> str:
    """Token a dry run hands out; the real call must echo it, proving the user saw that exact change."""
    payload = json.dumps([target.describe(), *parts], sort_keys=True, default=str).encode()
    return hmac.new(_TOKEN_KEY, payload, hashlib.sha256).hexdigest()[:16]


def require_confirm_token(target: Target, token: str | None, *parts: Any) -> None:
    if not token or not hmac.compare_digest(token, confirm_token(target, *parts)):
        raise AdfMcpError(
            "confirmation_required",
            "This change needs the confirm_token from a dry run of the same change.",
            "Call again with dry_run=true, show the user the result, then pass its confirm_token.",
        )


def audit(target: Target, operation: str, resource: str, details: dict | None = None) -> None:
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "os_user": getpass.getuser(),
        "operation": operation,
        "resource": resource,
        **target.describe(),
        **({"details": details} if details else {}),
    }
    with (home_dir() / "audit.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
