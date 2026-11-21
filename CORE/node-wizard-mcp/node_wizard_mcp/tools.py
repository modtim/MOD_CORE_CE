"""The mechanical tool set exposed by the Node-Builder MCP server.

Each tool is a thin wrapper over a `NodeAuthoringClient` call. There is
deliberately NO codegen / describe / synthesize / containerize tool — the
client agent writes the Dockerfile and iterates using `validate_submission`
feedback on its own model and token budget. This keeps the boundary
crisp: MOD ships tools, not a server-side LLM.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .client import NodeAuthoringClient

# The complete, fixed tool set. Anything not here is not exposed.
MECHANICAL_TOOL_NAMES: List[str] = [
    "get_node_contract",
    "list_base_images",
    "validate_submission",
    "build_node",
    "register_node",
    "get_build_status",
]

# Guard rails: substrings that must NEVER appear in an exposed tool name — they
# would signal a server-side codegen capability leaking into the free MCP.
FORBIDDEN_TOOL_SUBSTRINGS: List[str] = [
    "describe", "synthesize", "synthesise", "generate", "codegen",
    "containerize", "containerise", "adapt", "llm", "wizard",
]


def _manifest(name: str, type: str, **fields: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"name": name, "type": type}
    for k, v in fields.items():
        if v is not None:
            body[k] = v
    return body


def build_tools(client: NodeAuthoringClient) -> Dict[str, Callable[..., Dict[str, Any]]]:
    """Bind the tool implementations to a client. Returns {name: callable}.

    The returned callables are plain functions (unit-testable without the MCP
    runtime); `server.py` registers them as MCP tools.
    """

    def get_node_contract() -> Dict[str, Any]:
        """Fetch the versioned node-contract reference (input/output, env config,
        allowlisted base image, non-root, manifest fields, examples)."""
        return client.contract()

    def list_base_images() -> Dict[str, Any]:
        """List the base-image allowlist an author must build against."""
        return client.base_images()

    def validate_submission(submission: Dict[str, Any]) -> Dict[str, Any]:
        """Dry-run admission on a candidate submission. Returns {passed, rule,
        reason} and creates nothing. `submission` is the same body shape as
        register_node / build_node."""
        return client.validate(submission)

    def register_node(
        image: str,
        name: str,
        type: str,
        description: Optional[str] = None,
        input_schema: Optional[Dict[str, Any]] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        configuration_schema: Optional[Dict[str, Any]] = None,
        accepted_formats: Optional[List[str]] = None,
        cost_in_credits: Optional[int] = None,
        batch_mode: Optional[str] = None,
        container_timeout: Optional[int] = None,
        allowed_egress: Optional[List[Dict[str, Any]]] = None,
        use_sandbox_runtime: Optional[bool] = None,
        gpu_variants: Optional[Dict[str, str]] = None,
        process_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register a pre-built, pullable image as a node (source='image').
        Returns the created node id + scan status (201) or the API's rejection."""
        body = _manifest(
            name, type, description=description, input_schema=input_schema,
            output_schema=output_schema, configuration_schema=configuration_schema,
            accepted_formats=accepted_formats, cost_in_credits=cost_in_credits,
            batch_mode=batch_mode, container_timeout=container_timeout,
            allowed_egress=allowed_egress, use_sandbox_runtime=use_sandbox_runtime,
            gpu_variants=gpu_variants, process_type=process_type,
        )
        body["source"] = "image"
        body["image"] = image
        return client.create_node(body)

    def build_node(
        dockerfile: str,
        name: str,
        type: str,
        description: Optional[str] = None,
        container_env: Optional[Dict[str, str]] = None,
        input_schema: Optional[Dict[str, Any]] = None,
        output_schema: Optional[Dict[str, Any]] = None,
        configuration_schema: Optional[Dict[str, Any]] = None,
        accepted_formats: Optional[List[str]] = None,
        cost_in_credits: Optional[int] = None,
        batch_mode: Optional[str] = None,
        container_timeout: Optional[int] = None,
        allowed_egress: Optional[List[Dict[str, Any]]] = None,
        use_sandbox_runtime: Optional[bool] = None,
        gpu_variants: Optional[Dict[str, str]] = None,
        process_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a self-contained Dockerfile then register it (source='dockerfile').
        Returns a build task id (202) to poll with get_build_status, or the
        API's rejection. No server-side codegen — the Dockerfile is yours."""
        body = _manifest(
            name, type, description=description, container_env=container_env,
            input_schema=input_schema, output_schema=output_schema,
            configuration_schema=configuration_schema, accepted_formats=accepted_formats,
            cost_in_credits=cost_in_credits, batch_mode=batch_mode,
            container_timeout=container_timeout, allowed_egress=allowed_egress,
            use_sandbox_runtime=use_sandbox_runtime, gpu_variants=gpu_variants,
            process_type=process_type,
        )
        body["source"] = "dockerfile"
        body["dockerfile"] = dockerfile
        return client.create_node(body)

    def get_build_status(task_id: int) -> Dict[str, Any]:
        """Poll a build task id returned by build_node."""
        return client.build_status(task_id)

    tools: Dict[str, Callable[..., Dict[str, Any]]] = {
        "get_node_contract": get_node_contract,
        "list_base_images": list_base_images,
        "validate_submission": validate_submission,
        "build_node": build_node,
        "register_node": register_node,
        "get_build_status": get_build_status,
    }
    # Invariant: exactly the mechanical set, nothing else.
    assert set(tools) == set(MECHANICAL_TOOL_NAMES)
    return tools
