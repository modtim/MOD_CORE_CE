"""Worker-Exec MCP server.

A thin MCP adapter over the MOD host-exec HTTP API
(`/v1/workers/{id}/exec`). It contains no orchestration logic of its own and
reaches submit/cancel/wait/status ONLY by calling the public API, carrying the
caller's API key through unchanged. Mechanical tools only — there is no shell,
no authoring or codegen tool here.
"""
from .client import WorkerExecClient
from .tools import EXEC_TOOL_NAMES, build_tools

__all__ = ["WorkerExecClient", "EXEC_TOOL_NAMES", "build_tools"]
