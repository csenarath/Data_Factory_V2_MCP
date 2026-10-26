"""Toolsets. Each module exposes ``register(mcp)``; ADF_MCP_TOOLSETS selects which load."""

from . import authoring, context, debug, health, monitor, triggers

TOOLSETS = {
    "context": context.register,
    "monitor": monitor.register,
    "health": health.register,
    "authoring": authoring.register,
    "triggers": triggers.register,
    "debug": debug.register,
}
ALWAYS_ON = {"context"}
