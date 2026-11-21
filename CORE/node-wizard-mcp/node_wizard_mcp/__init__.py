"""Node-Builder MCP server.

A thin MCP adapter over the MOD external node-authoring HTTP API
(`/v1/node-authoring/*`). It contains no node-registration logic of its own and
reaches building/registration/admission ONLY by calling the public API,
carrying the caller's API key through unchanged. It exposes mechanical tools
only — there is no server-side codegen tool; the client agent does all the
authoring reasoning on its own model.
"""
from .client import NodeAuthoringClient
from .tools import MECHANICAL_TOOL_NAMES, build_tools

__all__ = ["NodeAuthoringClient", "MECHANICAL_TOOL_NAMES", "build_tools"]
