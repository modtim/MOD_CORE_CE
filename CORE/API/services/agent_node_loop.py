"""Agent-node tool loop — workflow-expressiveness-primitives 4.1/4.3 (slice).

Pure, dependency-injected driver for the "agents as nodes" agent loop: the
LLM step and the tool dispatch are injected callables so the orchestrator
wiring (a later task) can supply real ones. NO network, NO DB, NO logging of
payloads here — the module is deterministic and side-effect free.

Spec: workflow-agents-as-nodes/spec.md — an agent node declares a `tools`
array, keeps conversation history across tool calls within ONE dispatch,
dispatches the LLM's chosen tool, and is bounded by `max_tool_calls`
(failure_reason=agent_max_tool_calls_exceeded, full history surfaced for
debugging).

The budget primitive + settings live in the committed
`services.agent_node_runner` (FAILURE_REASON_MAX_TOOL_CALLS,
effective_max_tool_calls, ToolCallBudget, AgentToolCallLimitExceeded);
callers pass `effective_max_tool_calls(node_config)` as `max_tool_calls`.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from services.agent_node_runner import (
    FAILURE_REASON_MAX_TOOL_CALLS,
    ToolCallBudget,
    AgentToolCallLimitExceeded,
)

# §14.1 EXPR.14.1 — agent-node tool-call metrics (best-effort, never changes
# behaviour). Paid-only module (1634d): on the CE carve it is None and the
# metric is skipped, which is what "best-effort" already means here.
from utils.paid_import import optional_paid
expressiveness_metrics = optional_paid("services.expressiveness_metrics")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FAILURE_REASON_INVALID_TOOLS = "agent_invalid_tools"
FAILURE_REASON_LLM_ERROR = "agent_llm_error"

MAX_TOOLS = 32
TOOL_REF_KINDS = frozenset({"node", "sub_workflow", "node_definition"})

TRUNCATION_MARKER = "[agent_node_loop: output truncated]"


@dataclass
class AgentLoopResult:
    """Outcome of one agent-node dispatch (never raises)."""
    status: str = "failed"        # "completed" | "failed" | "parked"
    output: Any = None
    failure_reason: str | None = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0


# ---------------------------------------------------------------------------
# Tool-schema derivation + config validation
# ---------------------------------------------------------------------------


def _validate_tools(node_config: Any) -> Any:
    """Return (tool_schemas, ref_by_name), or None when the config is invalid.

    A valid `tools` array is a list of at most MAX_TOOLS entries, each
    `{name, ref: {kind: node|sub_workflow|node_definition, id}}` with unique
    non-empty names. A missing/empty `tools` array is a valid no-tool agent.
    """
    if not isinstance(node_config, dict):
        return None
    tools = node_config.get("tools", [])
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        return None

    schemas: List[Dict[str, Any]] = []
    refs: Dict[str, Any] = {}
    for entry in tools:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        ref = entry.get("ref")
        if not isinstance(name, str) or not name or name in refs:
            return None
        if not isinstance(ref, dict):
            return None
        if ref.get("kind") not in TOOL_REF_KINDS or ref.get("id") is None:
            return None
        refs[name] = ref
        schemas.append({
            "name": name,
            "kind": ref["kind"],
            "ref_id": ref["id"],
            "description": entry.get("description")
                           or f"Declared agent tool {name} ({ref['kind']})",
            "parameters": deepcopy(entry.get("input_schema"))
                         if entry.get("input_schema") else {},
        })
    return schemas, refs


def _history_chars(history: List[Dict[str, Any]]) -> int:
    """Rough character footprint of the conversation history."""
    total = 0
    for entry in history:
        for value in entry.values():
            if isinstance(value, str):
                total += len(value)
            elif isinstance(value, dict):
                total += len(str(value))
        total += 8  # role/structure overhead
    return total


def _truncate_history(history: List[Dict[str, Any]], max_history_chars: int) -> None:
    """Bound the history: drop OLDEST tool output payloads first, in place.

    The user input and every assistant/tool envelope are never dropped —
    only the payload of a tool result, which is replaced by a marker and
    flagged `"truncated": True`.
    """
    if _history_chars(history) <= max_history_chars:
        return
    for entry in history:
        if entry.get("role") != "tool":
            continue
        if entry.get("truncated"):
            continue
        entry["output"] = TRUNCATION_MARKER
        entry["truncated"] = True
        if _history_chars(history) <= max_history_chars:
            return


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def run_agent_loop(
    *,
    node_config: dict,
    agent_input: Any,
    llm_step: Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], Dict[str, Any]],
    dispatch_tool: Callable[[Any, Dict[str, Any]], Dict[str, Any]],
    max_tool_calls: int,
    max_history_chars: int = 200_000,
    history: Optional[List[Dict[str, Any]]] = None,
) -> AgentLoopResult:
    """Drive one agent-node dispatch to a terminal output or a failure.

    `llm_step(history, tool_schemas)` returns either
    `{"type": "final", "output": ...}` or
    `{"type": "tool_call", "tool": name, "arguments": {...}}`.
    `dispatch_tool(tool_ref, arguments)` returns
    `{"ok": bool, "output": ..., "error": str|None}`, or
    `{"parked": True}` when the dispatch handed the node off to a child run.

    `history` (EXPR 4.4 slice A): when a non-empty list is supplied the loop
    uses THAT list object in place as its history — a seeded resume carries its
    own user/assistant/tool entries, so no fresh user message is prepended and
    the caller's list is mutated in place exactly as the fresh path's is.
    `None` keeps today's behaviour exactly.

    A `{"parked": True}` outcome ends the loop immediately: status="parked",
    output None, the current history and the tool-call count so far, with NO
    error entry appended and no tool-call metric recorded (the child's
    completion, not this dispatch, is the outcome).

    Never raises: an LLM failure becomes status=failed /
    failure_reason=agent_llm_error; a raising tool dispatch becomes a tool
    error entry in the history (class name only, no traceback); an
    over-budget agent becomes FAILURE_REASON_MAX_TOOL_CALLS with the FULL
    history returned so it can be debugged.
    """
    validated = _validate_tools(node_config)
    if validated is None:
        return AgentLoopResult(
            status="failed",
            failure_reason=FAILURE_REASON_INVALID_TOOLS,
            history=[],
            tool_calls=0,
        )
    tool_schemas, refs = validated
    if history is None:
        history = [{"role": "user", "content": agent_input}]
    elif not history:
        # An empty caller-owned list: same content as a fresh dispatch, but the
        # caller's list object stays live so a park stamp can deepcopy it.
        history.append({"role": "user", "content": agent_input})

    try:
        budget = ToolCallBudget(max(1, int(max_tool_calls)))
    except (TypeError, ValueError):
        budget = ToolCallBudget(1)

    tool_calls = 0
    while True:
        try:
            step = llm_step(history, tool_schemas)
        except Exception:
            return AgentLoopResult(
                status="failed",
                failure_reason=FAILURE_REASON_LLM_ERROR,
                history=history,
                tool_calls=tool_calls,
            )

        if not isinstance(step, dict):
            return AgentLoopResult(
                status="failed",
                failure_reason=FAILURE_REASON_LLM_ERROR,
                history=history,
                tool_calls=tool_calls,
            )

        step_type = step.get("type")
        if step_type == "final":
            return AgentLoopResult(
                status="completed",
                output=step.get("output"),
                failure_reason=None,
                history=history,
                tool_calls=tool_calls,
            )
        if step_type != "tool_call":
            return AgentLoopResult(
                status="failed",
                failure_reason=FAILURE_REASON_LLM_ERROR,
                history=history,
                tool_calls=tool_calls,
            )

        tool_name = step.get("tool")
        arguments = step.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}

        try:
            budget.consume()
        except AgentToolCallLimitExceeded:
            # The agent wanted one more call than its cap: surface the FULL
            # history (spec scenario "Agent loops indefinitely — cap fires").
            return AgentLoopResult(
                status="failed",
                failure_reason=FAILURE_REASON_MAX_TOOL_CALLS,
                history=history,
                tool_calls=tool_calls,
            )

        tool_calls += 1
        history.append({
            "role": "assistant",
            "tool_call": {"tool": tool_name, "arguments": arguments},
        })

        ref = refs.get(tool_name) if isinstance(tool_name, str) else None
        if ref is None:
            history.append({
                "role": "tool",
                "tool": tool_name,
                "ok": False,
                "output": None,
                "error": f"unknown tool: {tool_name}",
            })
            _truncate_history(history, max_history_chars)
            continue

        try:
            outcome = dispatch_tool(ref, arguments)
        except Exception as exc:
            outcome = {"ok": False, "output": None,
                       "error": f"dispatch_tool raised {type(exc).__name__}"}
        if not isinstance(outcome, dict):
            outcome = {"ok": False, "output": None,
                       "error": "dispatch_tool returned a non-dict result"}

        if outcome.get("parked") is True:
            # EXPR 4.4 slice A: the node was handed off to a child run. The
            # assistant tool_call entry is already in the history (the resume
            # side appends the tool RESULT); nothing else is written here and
            # the child's completion — not this dispatch — is the outcome.
            return AgentLoopResult(
                status="parked",
                output=None,
                failure_reason=None,
                history=history,
                tool_calls=tool_calls,
            )

        # §14.1 EXPR.14.1: count the normalized dispatch outcome (best-effort;
        # skipped on the CE carve, where the metrics module is absent).
        if expressiveness_metrics is not None:
            try:
                expressiveness_metrics.record_agent_tool_call(
                    "ok" if bool(outcome.get("ok")) else "error"
                )
            except Exception:  # noqa: BLE001 — metrics never change behaviour
                pass

        history.append({
            "role": "tool",
            "tool": tool_name,
            "ok": bool(outcome.get("ok")),
            "output": outcome.get("output"),
            "error": outcome.get("error"),
        })
        _truncate_history(history, max_history_chars)
