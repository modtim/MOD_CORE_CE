"""Workflow-Wizard MCP server (mechanical core).

A thin MCP adapter over the MECHANICAL subset of the MOD Workflow Wizard
(`/v1/workflow-authoring/*`): apply a session's stored proposal, sandbox
test-run + status, queue missing node builds, record an audit action. The
server-side LLM generate/turn is NOT exposed and NOT reachable — the client
agent brings its own LLM.
"""
from .client import WorkflowAuthoringClient
from .tools import MECHANICAL_TOOL_NAMES, build_tools

__all__ = ["WorkflowAuthoringClient", "MECHANICAL_TOOL_NAMES", "build_tools"]
