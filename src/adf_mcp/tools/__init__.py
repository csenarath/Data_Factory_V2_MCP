"""Toolsets. Each module exposes ``register(mcp)``; ADF_MCP_TOOLSETS selects which load."""

from . import admin, authoring, context, cost, debug, health, monitor, triggers

TOOLSETS = {
    "context": context.register,
    "monitor": monitor.register,
    "health": health.register,
    "authoring": authoring.register,
    "triggers": triggers.register,
    "debug": debug.register,
    "admin": admin.register,
    "cost": cost.register,
}
ALWAYS_ON = {"context"}
