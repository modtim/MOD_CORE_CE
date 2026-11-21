"""Agent node runner — max_tool_calls cap primitive for 4.3.

The agent node handler (4.1) is not yet implemented; this module
provides the budget primitive + settings so 4.1 can plug in.

Spec: workflow-agents-as-nodes/spec.md — agent nodes are bounded by
max_tool_calls cap with failure_reason=agent_max_tool_calls_exceeded.
"""
from __future__ import annotations

from datetime import datetime, timezone

from services import platform_settings as psr

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GROUP = "agent"

FAILURE_REASON_MAX_TOOL_CALLS = "agent_max_tool_calls_exceeded"

# Default from the expressiveness spec (task 4.3): 20 tool calls.
DEFAULT_MAX_TOOL_CALLS: int = 20

# Hard ceiling — no tenant can request more than this.
DEFAULT_CEILING: int = 50

# ---------------------------------------------------------------------------
# Settings registration (at import time)
# ---------------------------------------------------------------------------

_AGENT_MAX_TOOL_CALLS_DEFAULT = "agent.max_tool_calls_default"
_AGENT_MAX_TOOL_CALLS_CEILING = "agent.max_tool_calls_ceiling"


def _register_settings() -> None:
    psr.register(
        _AGENT_MAX_TOOL_CALLS_DEFAULT,
        int,
        DEFAULT_MAX_TOOL_CALLS,
        group=GROUP,
        description="workflow-expressiveness-primitives 4.3 — default "
                    "max_tool_calls cap for agent nodes. Default 20; "
                    "a tenant row can lower/raise it.",
    )
    psr.register(
        _AGENT_MAX_TOOL_CALLS_CEILING,
        int,
        DEFAULT_CEILING,
        group=GROUP,
        description="workflow-expressiveness-primitives 4.3 — hard upper "
                    "bound on agent max_tool_calls. Default 50. Never "
                    "exceeded regardless of tenant override.",
    )


_register_settings()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def effective_max_tool_calls(node_config: dict, db=None) -> int:
    """Return the effective max_tool_calls for an agent node.

    Priority: node_config value (if present & valid) → DB setting → module
    default, all clamped to [1, ceiling].

    Settings read failures fall through to module defaults.
    """
    def _fallback() -> int:
        """Read default from settings, then clamp to [1, ceiling]."""
        ceiling = _read_ceiling(db)
        default = _read_default(db)
        return max(1, min(default, ceiling))

    try:
        val = node_config.get("max_tool_calls")
        if val is None:
            return _fallback()
        val = int(val)  # coerce (may raise)
        if val < 0:
            return _fallback()  # negative = invalid
        if val == 0:
            val = 1  # clamp 0 to 1
        ceiling = _read_ceiling(db)
        return min(val, ceiling)
    except (TypeError, ValueError):
        return _fallback()


def _read_default(db=None) -> int:
    """Read the platform-level default from the settings registry."""
    try:
        return psr.get_setting(_AGENT_MAX_TOOL_CALLS_DEFAULT, db)
    except Exception:
        return DEFAULT_MAX_TOOL_CALLS


def _read_ceiling(db=None) -> int:
    """Read the platform-level ceiling from the settings registry."""
    try:
        return psr.get_setting(_AGENT_MAX_TOOL_CALLS_CEILING, db)
    except Exception:
        return DEFAULT_CEILING


# ---------------------------------------------------------------------------
# Budget class
# ---------------------------------------------------------------------------


class AgentToolCallLimitExceeded(Exception):
    """Raised when the agent would exceed its tool call limit."""

    def __init__(
        self,
        failure_reason: str,
        limit: int,
        used: int,
    ) -> None:
        self.failure_reason = failure_reason
        self.limit = limit
        self.used = used
        super().__init__(
            f"{failure_reason}: limit={limit}, used={used}"
        )


class ToolCallBudget:
    """Track tool call usage for an agent node dispatch.

    Raise AgentToolCallLimitExceeded on consume() when the next call
    would exceed the budget.
    """

    def __init__(self, max_calls: int) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        self._limit = max_calls
        self._used = 0

    @property
    def remaining(self) -> int:
        return self._limit - self._used

    @property
    def used(self) -> int:
        return self._used

    def consume(self) -> None:
        """Consume one tool call credit.

        Raises AgentToolCallLimitExceeded when the next call exceeds
        the limit.
        """
        if self._used >= self._limit:
            raise AgentToolCallLimitExceeded(
                failure_reason=FAILURE_REASON_MAX_TOOL_CALLS,
                limit=self._limit,
                used=self._used,
            )
        self._used += 1
