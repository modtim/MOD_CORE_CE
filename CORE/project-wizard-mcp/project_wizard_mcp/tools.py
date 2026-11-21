"""The mechanical tool set exposed by the Project-Wizard MCP server.

One mechanical operation: record the grading example a project session is built
against. There is deliberately NO turn / tick / wake / generate tool — those are
the LLM-driven agentic loop, which is the client agent's job on its own model.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .client import ProjectAuthoringClient

MECHANICAL_TOOL_NAMES: List[str] = [
    "record_criteria",
]

FORBIDDEN_TOOL_SUBSTRINGS: List[str] = [
    "describe", "synthesize", "synthesise", "generate", "codegen",
    "containerize", "containerise", "adapt", "llm", "wizard",
    "turn", "tick", "wake", "chat",
]


def build_tools(client: ProjectAuthoringClient) -> Dict[str, Callable[..., Dict[str, Any]]]:
    def record_criteria(session_id: str, input_filename: str, input_body_b64: str,
                        described_outcome: Optional[str] = None,
                        output_filename: Optional[str] = None,
                        output_body_b64: Optional[str] = None) -> Dict[str, Any]:
        """Record the grading example (input + expected output) a project session
        is built and graded against. Returns {ready_to_build, message}. No LLM."""
        body: Dict[str, Any] = {
            "session_id": session_id,
            "input_filename": input_filename,
            "input_body_b64": input_body_b64,
        }
        for k, v in (("described_outcome", described_outcome),
                     ("output_filename", output_filename),
                     ("output_body_b64", output_body_b64)):
            if v is not None:
                body[k] = v
        return client.record_criteria(body)

    tools: Dict[str, Callable[..., Dict[str, Any]]] = {
        "record_criteria": record_criteria,
    }
    assert set(tools) == set(MECHANICAL_TOOL_NAMES)
    return tools
