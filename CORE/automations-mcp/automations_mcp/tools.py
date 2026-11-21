"""The mechanical tool set exposed by the Automations MCP server.

Full trigger lifecycle (this surface legitimately creates/updates/deletes
triggers) plus read-only observability (trigger event history, run log). No
codegen/LLM tool, and no raw platform API-log-stream tool.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .client import AutomationsClient

MECHANICAL_TOOL_NAMES: List[str] = [
    "create_trigger",
    "list_triggers",
    "get_trigger",
    "update_trigger",
    "delete_trigger",
    "rotate_trigger_secret",
    "get_trigger_events",
    "get_run_log",
]

# NB: create/update/delete are intentionally NOT forbidden here — trigger
# management is this tool's whole point. The guard still bans codegen/LLM.
FORBIDDEN_TOOL_SUBSTRINGS: List[str] = [
    "describe", "synthesize", "synthesise", "generate", "codegen",
    "containerize", "containerise", "adapt", "llm", "wizard",
]


def build_tools(client: AutomationsClient) -> Dict[str, Callable[..., Dict[str, Any]]]:
    def create_trigger(workflow_id: int, trigger_type: str,
                       configuration: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Create a trigger (schedule/webhook/file_event/workflow_completion/
        log_pattern_alert/queue_empty/interval/one_shot/email/git/api_call/form)
        for a workflow you own. Webhook/form types return a public URL + secret
        exactly once."""
        body: Dict[str, Any] = {"workflow_id": workflow_id, "trigger_type": trigger_type}
        if configuration is not None:
            body["configuration"] = configuration
        return client.create_trigger(body)

    def list_triggers(workflow_id: Optional[int] = None) -> Dict[str, Any]:
        """List your tenant's triggers (optionally filtered by workflow_id)."""
        return client.list_triggers(workflow_id)

    def get_trigger(trigger_id: int) -> Dict[str, Any]:
        """Get one trigger you own."""
        return client.get_trigger(trigger_id)

    def update_trigger(trigger_id: int, configuration: Optional[Dict[str, Any]] = None,
                      is_active: Optional[bool] = None) -> Dict[str, Any]:
        """Update a trigger's configuration and/or active flag."""
        body: Dict[str, Any] = {}
        if configuration is not None:
            body["configuration"] = configuration
        if is_active is not None:
            body["is_active"] = is_active
        return client.update_trigger(trigger_id, body)

    def delete_trigger(trigger_id: int) -> Dict[str, Any]:
        """Soft-delete a trigger you own."""
        return client.delete_trigger(trigger_id)

    def rotate_trigger_secret(trigger_id: int) -> Dict[str, Any]:
        """Rotate a webhook/form trigger's secret. Plaintext + URL returned once."""
        return client.rotate_secret(trigger_id)

    def get_trigger_events(trigger_id: int, limit: int = 50) -> Dict[str, Any]:
        """Recent event-arrival history for a trigger (what arrived, when, and
        dedup/debounce state). Read-only, tenant-scoped."""
        return client.trigger_events(trigger_id, limit)

    def get_run_log(run_id: int) -> Dict[str, Any]:
        """Detailed per-node execution log for one of your workflow runs."""
        return client.run_log(run_id)

    tools: Dict[str, Callable[..., Dict[str, Any]]] = {
        "create_trigger": create_trigger,
        "list_triggers": list_triggers,
        "get_trigger": get_trigger,
        "update_trigger": update_trigger,
        "delete_trigger": delete_trigger,
        "rotate_trigger_secret": rotate_trigger_secret,
        "get_trigger_events": get_trigger_events,
        "get_run_log": get_run_log,
    }
    assert set(tools) == set(MECHANICAL_TOOL_NAMES)
    return tools
