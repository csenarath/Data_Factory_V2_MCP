"""Structured errors: every failure reaches Claude as {code, message, hint}."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import Any, TypeVar

from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ResourceNotFoundError
from azure.identity import AuthenticationRequiredError, CredentialUnavailableError
from mcp.server.mcpserver.exceptions import ToolError

F = TypeVar("F", bound=Callable[..., Any])


class AdfMcpError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_tool_error(self) -> ToolError:
        body = {"code": self.code, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        return ToolError(json.dumps(body))


def _from_http(exc: HttpResponseError) -> AdfMcpError:
    status = exc.status_code or 0
    message = (exc.message or str(exc)).splitlines()[0][:1000]
    if status == 401:
        return AdfMcpError("unauthenticated", message, "Call adf_login, or check the profile's credentials.")
    if status == 403:
        return AdfMcpError(
            "forbidden",
            message,
            "The identity lacks an RBAC role on this scope. Monitoring needs Reader (or Data Factory "
            "Contributor for runs/cancel); authoring needs Data Factory Contributor.",
        )
    if status == 404 or isinstance(exc, ResourceNotFoundError):
        return AdfMcpError(
            "not_found", message, "Check the name, resource group and factory (adf_context shows the active target)."
        )
    if status == 429:
        return AdfMcpError("throttled", message, "ARM throttled the request; wait and retry with a narrower query.")
    return AdfMcpError(f"http_{status}" if status else "azure_error", message)


def handle_errors(fn: F) -> F:
    """Convert known exceptions into ToolErrors carrying a JSON {code, message, hint} body."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except AdfMcpError as exc:
            raise exc.to_tool_error() from exc
        except AuthenticationRequiredError as exc:
            raise AdfMcpError(
                "login_required", "Interactive sign-in is required for this profile.", "Call adf_login first."
            ).to_tool_error() from exc
        except (ClientAuthenticationError, CredentialUnavailableError) as exc:
            raise AdfMcpError(
                "auth_failed",
                str(exc).splitlines()[0][:500],
                "Call adf_login, or check the profile's auth settings (adf_context).",
            ).to_tool_error() from exc
        except HttpResponseError as exc:
            raise _from_http(exc).to_tool_error() from exc

    return wrapper  # type: ignore[return-value]
