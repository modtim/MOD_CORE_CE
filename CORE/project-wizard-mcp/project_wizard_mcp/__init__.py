"""Project-Wizard MCP server (mechanical core).

A thin MCP adapter over the MECHANICAL core of the MOD Project Wizard
(`/v1/project-authoring/*`). The only non-LLM operation the project wizard has
is recording the grading example (criteria); the conversational turn and the
agentic tick/wake pumping are LLM-driven and are NOT exposed. The client agent
brings its own LLM.
"""
from .client import ProjectAuthoringClient
from .tools import MECHANICAL_TOOL_NAMES, build_tools

__all__ = ["ProjectAuthoringClient", "MECHANICAL_TOOL_NAMES", "build_tools"]
