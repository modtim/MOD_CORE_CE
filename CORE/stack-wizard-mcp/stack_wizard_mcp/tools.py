"""The mechanical tool set exposed by the Stack MCP server.

List / create / get / start / stop persistent stacks. No from-wizard, no
codegen — the compose spec is the caller's own.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .client import StackClient

MECHANICAL_TOOL_NAMES: List[str] = [
    "list_stacks",
    "create_stack",
    "get_stack",
    "start_stack",
    "stop_stack",
]

FORBIDDEN_TOOL_SUBSTRINGS: List[str] = [
    "describe", "synthesize", "synthesise", "generate", "codegen",
    "containerize", "containerise", "adapt", "llm", "wizard", "author",
]


def build_tools(client: StackClient) -> Dict[str, Callable[..., Dict[str, Any]]]:
    def list_stacks() -> Dict[str, Any]:
        """List the caller tenant's persistent stacks."""
        return client.list_stacks()

    def create_stack(name: str, compose_spec: Dict[str, Any],
                     **fields: Any) -> Dict[str, Any]:
        """Create a persistent stack from a compose spec (non-wizard). Extra
        manifest fields (home_zone, etc.) may be passed through."""
        body: Dict[str, Any] = {"name": name, "compose_spec": compose_spec}
        for k, v in fields.items():
            if v is not None:
                body[k] = v
        return client.create_stack(body)

    def get_stack(stack_id: int) -> Dict[str, Any]:
        """Get one of the caller's stacks (404 for a foreign stack)."""
        return client.get_stack(stack_id)

    def start_stack(stack_id: int, to_worker: Optional[str] = None) -> Dict[str, Any]:
        """Start (deploy) one of the caller's stacks."""
        return client.start_stack(stack_id, to_worker)

    def stop_stack(stack_id: int) -> Dict[str, Any]:
        """Stop one of the caller's stacks without tearing it down."""
        return client.stop_stack(stack_id)

    tools: Dict[str, Callable[..., Dict[str, Any]]] = {
        "list_stacks": list_stacks,
        "create_stack": create_stack,
        "get_stack": get_stack,
        "start_stack": start_stack,
        "stop_stack": stop_stack,
    }
    assert set(tools) == set(MECHANICAL_TOOL_NAMES)
    return tools
