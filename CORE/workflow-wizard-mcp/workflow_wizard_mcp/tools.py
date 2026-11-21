"""The mechanical tool set exposed by the Workflow-Wizard MCP server.

Apply a session's stored proposal, sandbox test-run + status, queue missing
node builds, record an audit action. There is deliberately NO generate / turn /
describe tool — the LLM reasoning is the client agent's, on its own model.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .client import WorkflowAuthoringClient

MECHANICAL_TOOL_NAMES: List[str] = [
    "apply_proposal",
    "test_run",
    "get_test_run_status",
    "queue_missing_nodes",
    "record_audit_action",
]

FORBIDDEN_TOOL_SUBSTRINGS: List[str] = [
    "describe", "synthesize", "synthesise", "generate", "codegen",
    "containerize", "containerise", "adapt", "llm", "wizard", "turn", "chat",
]


def build_tools(client: WorkflowAuthoringClient) -> Dict[str, Callable[..., Dict[str, Any]]]:
    def apply_proposal(session_id: str, name: Optional[str] = None,
                       **merge_fields: Any) -> Dict[str, Any]:
        """Materialize the session's most-recent stored proposal into a workflow
        version. Extra merge-mode fields pass through. No LLM."""
        body: Dict[str, Any] = {"session_id": session_id}
        if name is not None:
            body["name"] = name
        for k, v in merge_fields.items():
            if v is not None:
                body[k] = v
        return client.apply(body)

    def test_run(session_id: str, proposal_index: Optional[int] = None) -> Dict[str, Any]:
        """Sandbox test-run of a session proposal. Returns a run handle."""
        body: Dict[str, Any] = {"session_id": session_id}
        if proposal_index is not None:
            body["proposal_index"] = proposal_index
        return client.test_run(body)

    def get_test_run_status(run_id: int) -> Dict[str, Any]:
        """Status of a sandbox test-run."""
        return client.test_run_status(run_id)

    def queue_missing_nodes(session_id: str, missing: List[Any]) -> Dict[str, Any]:
        """Queue container builds for a proposal's missing nodes."""
        return client.queue_missing({"session_id": session_id, "missing": missing})

    def record_audit_action(audit_session_id: str, finding_id: str,
                           severity: str, action: str,
                           **fields: Any) -> Dict[str, Any]:
        """Record a user/agent action on a wizard audit finding."""
        body: Dict[str, Any] = {
            "audit_session_id": audit_session_id, "finding_id": finding_id,
            "severity": severity, "action": action,
        }
        for k, v in fields.items():
            if v is not None:
                body[k] = v
        return client.audit_action(body)

    tools: Dict[str, Callable[..., Dict[str, Any]]] = {
        "apply_proposal": apply_proposal,
        "test_run": test_run,
        "get_test_run_status": get_test_run_status,
        "queue_missing_nodes": queue_missing_nodes,
        "record_audit_action": record_audit_action,
    }
    assert set(tools) == set(MECHANICAL_TOOL_NAMES)
    return tools
