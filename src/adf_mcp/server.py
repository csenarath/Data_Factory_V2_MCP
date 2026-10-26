"""MCP server assembly."""

from __future__ import annotations

import argparse
import logging
import os

from mcp.server.mcpserver import MCPServer

from .tools import ALWAYS_ON, TOOLSETS

INSTRUCTIONS = """\
Azure Data Factory (ADF v2) server. Tools are prefixed adf_.
- Start with adf_context to see the signed-in identity and target factory. If it reports an auth
  error, call adf_login (device-code sign-in returns instructions you must show the user).
- adf_profiles_list / adf_profile_use switch tenant/subscription/factory for this session only;
  adf_factories_list + adf_context_set target another factory in the same tenant.
- For failures: adf_failures_summary -> adf_run_tree -> adf_activity_io.
- Run history is retained 45 days. Write tools are blocked on read_only and prod profiles.
- Git-connected factories: definition changes go to Git, never to live mode.
"""


def selected_toolsets(raw: str | None) -> list[str]:
    if not raw or raw.strip().lower() == "all":
        return list(TOOLSETS)
    wanted = {name.strip() for name in raw.split(",") if name.strip()} | ALWAYS_ON
    unknown = wanted - set(TOOLSETS)
    if unknown:
        raise SystemExit(f"Unknown toolsets {sorted(unknown)}; available: {sorted(TOOLSETS)}")
    return [name for name in TOOLSETS if name in wanted]


def build_server(toolsets: list[str] | None = None) -> MCPServer:
    mcp = MCPServer(name="adf-mcp", instructions=INSTRUCTIONS)
    for name in toolsets or list(TOOLSETS):
        TOOLSETS[name](mcp)
    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="adf-mcp", description="Azure Data Factory MCP server")
    parser.add_argument("--toolsets", default=os.environ.get("ADF_MCP_TOOLSETS"), help="comma list or 'all'")
    parser.add_argument("--profile", help="profile to start on (overrides ADF_MCP_PROFILE)")
    args = parser.parse_args()
    if args.profile:
        os.environ["ADF_MCP_PROFILE"] = args.profile
    # The Azure SDK logs every HTTP request/response at INFO.
    logging.getLogger("azure").setLevel(os.environ.get("ADF_MCP_AZURE_LOG_LEVEL", "WARNING"))
    build_server(selected_toolsets(args.toolsets)).run("stdio")
