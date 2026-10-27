"""Background jobs, so no tool call blocks for long.

Long operations (debug runs, publish, connection tests, IR start/stop, ...) run in a daemon thread of the
server process. The tool waits at most ADF_MCP_MAX_WAIT_SECONDS (default 50) and otherwise returns a
job_id; adf_job_status picks it up later. Jobs live as long as the server process (= the Claude session).
"""

from __future__ import annotations

import os
import secrets
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .errors import AdfMcpError


def max_wait() -> float:
    return float(os.environ.get("ADF_MCP_MAX_WAIT_SECONDS", "50"))


def bounded(wait_seconds: float) -> float:
    """A wait a single tool call may spend, never more than max_wait()."""
    return max(0.0, min(float(wait_seconds), max_wait()))


@dataclass
class Job:
    id: str
    kind: str
    description: str
    started: str
    status: str = "running"  # running | succeeded | failed
    result: Any = None
    error: dict | None = None
    progress: list[str] = field(default_factory=list)
    finished: str | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def log(self, message: str) -> None:
        self.progress.append(f"{datetime.now(UTC):%H:%M:%S} {message}")

    def view(self, progress_lines: int = 10) -> dict:
        out: dict = {"job_id": self.id, "kind": self.kind, "description": self.description, "status": self.status, "started": self.started}
        if self.finished:
            out["finished"] = self.finished
        if self.progress:
            out["progress"] = self.progress[-progress_lines:]
        if self.status == "succeeded":
            out["result"] = self.result
        if self.error:
            out["error"] = self.error
        return out


class Jobs:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def start(self, kind: str, description: str, fn: Callable[[Job], Any]) -> Job:
        job = Job(id=f"{kind}-{secrets.token_hex(4)}", kind=kind, description=description, started=datetime.now(UTC).isoformat(timespec="seconds"))
        self._jobs[job.id] = job

        def run() -> None:
            try:
                job.result = fn(job)
                job.status = "succeeded"
            except AdfMcpError as exc:
                job.status, job.error = "failed", {"code": exc.code, "message": exc.message, "hint": exc.hint}
            except Exception as exc:  # surfaced through adf_job_status
                job.status = "failed"
                job.error = {"code": type(exc).__name__, "message": str(exc).splitlines()[0][:1000] if str(exc) else repr(exc)}
                job.log("".join(traceback.format_exception_only(exc)).strip()[:500])
            finally:
                job.finished = datetime.now(UTC).isoformat(timespec="seconds")
                job.done.set()

        threading.Thread(target=run, daemon=True, name=job.id).start()
        return job

    def get(self, job_id: str) -> Job:
        if job_id not in self._jobs:
            raise AdfMcpError("not_found", f"No job {job_id} in this session.", "Jobs end with the server process; call adf_jobs_list.")
        return self._jobs[job_id]

    def all(self) -> list[Job]:
        return list(self._jobs.values())


def run_job(jobs: Jobs, kind: str, description: str, fn: Callable[[Job], Any], wait_seconds: float) -> dict:
    """Start `fn` as a job and wait up to bounded(wait_seconds). Returns the result, or a running-job handle."""
    job = jobs.start(kind, description, fn)
    job.done.wait(bounded(wait_seconds))
    if job.status == "succeeded":
        result = job.result if isinstance(job.result, dict) else {"result": job.result}
        return {**result, "job_id": job.id}
    if job.status == "failed":
        raise AdfMcpError(job.error["code"], job.error["message"], job.error.get("hint"))
    return {
        **job.view(progress_lines=5),
        "next": f"Still running in the background. Call adf_job_status job_id={job.id} (waits up to {int(max_wait())}s per call).",
    }
