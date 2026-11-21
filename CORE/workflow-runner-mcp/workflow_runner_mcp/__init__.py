"""Workflow-Runner MCP server.

A thin MCP adapter over the MOD mechanical workflow-run HTTP API
(`/v1/workflow-runner/*`). It contains no orchestration logic of its own and
reaches run/cancel/restart/status ONLY by calling the public API, carrying the
caller's API key through unchanged. Mechanical tools only — there is no
workflow-authoring or codegen tool here.
"""
from .client import WorkflowRunnerClient
from .tools import MECHANICAL_TOOL_NAMES, build_tools

__all__ = ["WorkflowRunnerClient", "MECHANICAL_TOOL_NAMES", "build_tools"]
