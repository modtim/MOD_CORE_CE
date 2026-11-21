"""Automations MCP server.

A thin MCP adapter over the MOD automations API (`/v1/automations/*`): the full
workflow-trigger lifecycle (create/list/get/update/delete/rotate-secret across
every trigger type) plus read-only observability — a trigger's event-arrival
history and a run's detailed log. Carries the caller's API key through
unchanged. Mechanical only; the raw platform API-log stream is NOT exposed, and
there is no codegen tool.
"""
from .client import AutomationsClient
from .tools import MECHANICAL_TOOL_NAMES, build_tools

__all__ = ["AutomationsClient", "MECHANICAL_TOOL_NAMES", "build_tools"]
