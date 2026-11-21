"""The mechanical tool set exposed by the Workflow-Runner MCP server.

Each tool is a thin wrapper over a `WorkflowRunnerClient` call. There is
deliberately NO authoring / generate / describe tool — this surface only runs,
cancels, restarts, and reads existing workflows. The client agent decides what
to run on its own model and token budget.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from .client import WorkflowRunnerClient

# The complete, fixed MECHANICAL tool set — always exposed, unchanged. The only
# additions are dynamic exposed-workflow tools the API lists for the caller
# (mcpt.a7); nothing else is ever exposed.
MECHANICAL_TOOL_NAMES: List[str] = [
    "run_workflow_version",
    "cancel_run",
    "restart_run",
    "get_run_status",
    "get_run_progress",
    "get_run_node_status",
    "get_run_cost",
]

# Guard rails: substrings that must NEVER appear in an exposed tool name — they
# would signal an authoring / server-side codegen capability leaking in.
FORBIDDEN_TOOL_SUBSTRINGS: List[str] = [
    "describe", "synthesize", "synthesise", "generate", "codegen",
    "containerize", "containerise", "adapt", "llm", "wizard", "author",
]


def build_tools(client: WorkflowRunnerClient) -> Dict[str, Callable[..., Dict[str, Any]]]:
    """Bind the tool implementations to a client. Returns {name: callable}."""

    def run_workflow_version(
        version_id: int,
        cog_ids: List[int],
        metadata_cog_ids: Optional[List[int]] = None,
        parent_folder_id: Optional[int] = None,
        extra_variables: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run a workflow version over the given input file ids (cog_ids).
        Returns a 202 run handle, or the API's rejection. The caller's tenant
        must have Harbor credentials configured."""
        body: Dict[str, Any] = {"cog_ids": cog_ids}
        if metadata_cog_ids is not None:
            body["metadata_cog_ids"] = metadata_cog_ids
        if parent_folder_id is not None:
            body["parent_folder_id"] = parent_folder_id
        if extra_variables is not None:
            body["extra_variables"] = extra_variables
        return client.run_version(version_id, body)

    def cancel_run(run_id: int) -> Dict[str, Any]:
        """Cancel a running or queued run in the caller's tenant."""
        return client.cancel(run_id)

    def restart_run(run_id: int, resume_mode: Optional[str] = None) -> Dict[str, Any]:
        """Restart a finished run as a new run. resume_mode: 'full' (default)
        or 'failed_only'."""
        body: Dict[str, Any] = {}
        if resume_mode is not None:
            body["resume_mode"] = resume_mode
        return client.restart(run_id, body)

    def get_run_status(run_id: int) -> Dict[str, Any]:
        """DB-backed status for a run in the caller's tenant."""
        return client.status(run_id)

    def get_run_progress(run_id: int) -> Dict[str, Any]:
        """Progress (percent complete, current node, ETA) for a run."""
        return client.progress(run_id)

    def get_run_node_status(run_id: int) -> Dict[str, Any]:
        """Per-node execution status for a run."""
        return client.node_status(run_id)

    def get_run_cost(run_id: int) -> Dict[str, Any]:
        """Per-node cost breakdown for a run."""
        return client.cost(run_id)

    static: Dict[str, Callable[..., Dict[str, Any]]] = {
        "run_workflow_version": run_workflow_version,
        "cancel_run": cancel_run,
        "restart_run": restart_run,
        "get_run_status": get_run_status,
        "get_run_progress": get_run_progress,
        "get_run_node_status": get_run_node_status,
        "get_run_cost": get_run_cost,
    }
    # Invariant: the 7 mechanical tools are always present, unchanged.
    assert set(static) == set(MECHANICAL_TOOL_NAMES)

    tools: Dict[str, Callable[..., Dict[str, Any]]] = dict(static)
    for entry in client.exposed_tools():
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in static:
            logging.warning("exposed-workflow tool %s collides with a static tool; skipped", name)
            continue
        low = name.lower()
        if any(bad in low for bad in FORBIDDEN_TOOL_SUBSTRINGS):
            logging.warning("exposed-workflow tool %s hits an authoring/codegen guard rail; skipped", name)
            continue
        tools[name] = _bind_exposed(client, name)
    return tools


def _bind_exposed(client: WorkflowRunnerClient, name: str) -> Callable[..., Dict[str, Any]]:
    """A dynamic tool: agent kwargs -> POST /v1/mcp-tools/tools/call.

    The result is the client's normal ApiResult, so API rejections (400 schema
    mismatch, 404 unknown/disabled tool) surface as tool results, not exceptions.
    """

    def call(**arguments: Any) -> Dict[str, Any]:
        return client.call_exposed_workflow(name, arguments)

    return call
