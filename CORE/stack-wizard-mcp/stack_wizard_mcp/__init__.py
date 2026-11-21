"""Stack MCP server.

A thin MCP adapter over the MOD mechanical persistent-stack lifecycle API
(`/v1/stack/*`): list / create / get / start / stop. Carries the caller's API
key through unchanged. Mechanical only — the LLM-backed stack wizard
(`/persistent-stacks/from-wizard`) is NOT reachable here, and there is no
codegen tool.
"""
from .client import StackClient
from .tools import MECHANICAL_TOOL_NAMES, build_tools

__all__ = ["StackClient", "MECHANICAL_TOOL_NAMES", "build_tools"]
