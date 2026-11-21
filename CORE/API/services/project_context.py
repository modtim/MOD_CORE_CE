"""project-agent §2.3/§2.4 — what a project may forget, and what it must not.

Design decision D2. Holding all context centrally is the whole point of the
project agent: it is what lets the wizards stay cheap, because the node
wizard keeps receiving only expected input, the process needed, and expected
output while the reasoning about *why* stays with the parent.

The cost is that the agent's window becomes the ceiling on project size, and
a project running for weeks will exceed it. So something must be summarised,
and the decision that matters is what may not be.

NEVER summarised — these are what make the agent's next decision correct:

  - the plan and its acceptance criteria
  - the supplied example input and expected output
  - the inventory of what has been built
  - the record of what has been tried and failed, per task

Summarising a failure history is the sharp one: it makes the agent retry
what it already knows does not work, which is precisely the failure that
never-stop amplifies. A project that forgets its failures does not slow
down — it loops.

MAY be summarised:

  - conversation turns
  - superseded plan revisions
  - the narrative of work already completed and accepted

§2.4 — when the never-summarisable set ALONE approaches the budget, that is
reported rather than resolved by quietly dropping something. There is no
correct thing to drop at that point, and a project that silently forgets its
criteria produces confident wrong work.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Fields on SupportChatSession that carry project context which must
# survive summarisation. Ordered most- to least- load-bearing, which is
# also the order a ceiling report names them in.
DURABLE_FIELDS: Tuple[str, ...] = (
    "project_criteria",
    "project_plan",
    "project_failure_history",
    "project_artifacts",
    "project_need",
)

# Fields that may be compacted when space is short.
COMPACTABLE_FIELDS: Tuple[str, ...] = (
    "project_plan_history",
)

# Fraction of the budget at which the durable set alone is reported as a
# problem. Below this there is headroom for conversation; above it, the
# next thing to go would have to be something that must not go.
CEILING_WARN_RATIO = 0.75


def _rough_tokens(value: Any) -> int:
    """Cheap size estimate in tokens.

    Deliberately not a real tokeniser: this decides whether to WARN, and a
    warning that costs a model call to produce is a warning nobody enables.
    ~4 characters per token is close enough to be useful and never wrong by
    an order of magnitude on JSON.

    Base64 example payloads are counted by their reference rather than
    their bytes where possible — an inline 8 MB sample would otherwise
    dominate the estimate while never actually entering a prompt.
    """
    if value is None:
        return 0
    if isinstance(value, str):
        return max(1, len(value) // 4)
    try:
        return max(1, len(json.dumps(value, default=str)) // 4)
    except (TypeError, ValueError):
        return max(1, len(str(value)) // 4)


def _criteria_for_sizing(criteria: Any) -> Any:
    """Strip inline sample bodies before sizing.

    The example input/output are durable, but their BYTES never go into a
    prompt — only their shape and a reference do. Counting an 8 MB base64
    blob against the context budget would report a ceiling that does not
    exist.
    """
    if not isinstance(criteria, dict):
        return criteria
    out: Dict[str, Any] = {}
    for key, val in criteria.items():
        if isinstance(val, dict) and "body_b64" in val:
            trimmed = {k: v for k, v in val.items() if k != "body_b64"}
            trimmed["body_b64_bytes"] = len(val.get("body_b64") or "")
            out[key] = trimmed
        else:
            out[key] = val
    return out


def durable_size(session) -> Dict[str, int]:
    """Per-field token estimate of everything that may not be summarised."""
    sizes: Dict[str, int] = {}
    for field in DURABLE_FIELDS:
        value = getattr(session, field, None)
        if field == "project_criteria":
            value = _criteria_for_sizing(value)
        sizes[field] = _rough_tokens(value)
    return sizes


def ceiling_report(session, *, budget_tokens: int) -> Dict[str, Any]:
    """§2.4 — whether this project is running out of room, and where.

    Returns a dict that is safe to hand straight to a user: it names the
    fields by size so "your failure history is 60% of the budget" is
    answerable, rather than reporting an opaque percentage.
    """
    sizes = durable_size(session)
    total = sum(sizes.values())
    ratio = (total / budget_tokens) if budget_tokens > 0 else 0.0
    largest = max(sizes.items(), key=lambda kv: kv[1]) if sizes else (None, 0)
    return {
        "durable_tokens": total,
        "budget_tokens": budget_tokens,
        "ratio": round(ratio, 4),
        "at_ceiling": ratio >= CEILING_WARN_RATIO,
        "by_field": sizes,
        "largest_field": largest[0],
    }


def summarisable_payload(session) -> Dict[str, Any]:
    """The parts of a project that MAY be compacted, for a summariser.

    Transcript messages are not included: they live in
    support_chat_messages and are compacted by the existing chat path.
    This covers the project-row fields only.
    """
    return {
        field: getattr(session, field, None) for field in COMPACTABLE_FIELDS
    }


def assert_retention(before, after) -> None:
    """Guard for anything that rewrites a project session.

    Raises if a durable field changed shape in a way that loses content.
    Used by the summarisation path so a bug there fails loudly instead of
    quietly amputating the criteria and leaving the agent to produce
    confident wrong work.
    """
    for field in DURABLE_FIELDS:
        old = getattr(before, field, None)
        new = getattr(after, field, None)
        if old is None:
            continue
        if new is None:
            raise ValueError(
                f"summarisation dropped durable field {field!r} — "
                "project context that may not be summarised"
            )
        if _rough_tokens(new) < _rough_tokens(old):
            raise ValueError(
                f"summarisation shrank durable field {field!r} "
                f"({_rough_tokens(old)} -> {_rough_tokens(new)} tokens)"
            )


def record_failure(session, task_key: str, *, approach: str, rung: str,
                   reason: str, at: str) -> None:
    """Append to the per-task record of what has been tried.

    Append-only and never compacted. The agent reads this before choosing
    its next approach; losing it is how a never-stop loop starts repeating
    itself.
    """
    history: Dict[str, List[Dict[str, Any]]] = dict(
        session.project_failure_history or {}
    )
    entries = list(history.get(task_key) or [])
    entries.append({
        "approach": approach,
        "rung": rung,
        "reason": reason,
        "failed_at": at,
    })
    history[task_key] = entries
    session.project_failure_history = history


def tried_approaches(session, task_key: str) -> List[str]:
    """What has already been tried for a task, so a rung is not repeated."""
    history = session.project_failure_history or {}
    return [e.get("approach") for e in (history.get(task_key) or [])
            if e.get("approach")]


def record_attempt(session, task_key: str, *, rung: str, at: str,
                   approach: Optional[str] = None) -> None:
    """Record that an attempt at ``rung`` was DISPATCHED for this task.

    Written at the moment of dispatch, not at failure, and this is what
    makes the loop idempotent AND advancing: ``plan_next_attempt`` reads
    this history for both the attempt count (backoff, affordability) and
    which rungs have already been climbed. If a dispatch left no trace here
    the count stayed 0 and the ladder never moved off its first rung, so the
    next tick re-dispatched the same rung — the runaway the escalation
    ladder exists to prevent.

    Left ``reason=None``, ``status="dispatched"``: an attempt in flight has
    no outcome yet. ``settle_attempt`` fills the outcome when the build
    reports back, updating this same entry rather than appending a second,
    so a dispatched-then-failed attempt counts once.

    ``approach`` is left None at dispatch — the concrete approach is the
    wizard's to choose — so it does not pollute the ``avoid`` list the next
    attempt is told not to repeat; ``settle_attempt`` may fill it once known.
    """
    history: Dict[str, List[Dict[str, Any]]] = dict(
        session.project_failure_history or {}
    )
    entries = list(history.get(task_key) or [])
    entries.append({
        "approach": approach,
        "rung": rung,
        "reason": None,
        "status": "dispatched",
        "dispatched_at": at,
    })
    history[task_key] = entries
    session.project_failure_history = history


def settle_attempt(session, task_key: str, *, reason: str, at: str,
                   status: str = "failed",
                   approach: Optional[str] = None) -> None:
    """Fill in the outcome of the most recent DISPATCHED attempt.

    Updates the pending entry in place rather than appending, so a
    dispatched-then-failed attempt is ONE entry and the attempt count and
    the ladder do not double-count it. When no dispatched attempt is pending
    (a failure with no matching dispatch record), falls back to appending a
    standalone failure so the outcome is never lost.
    """
    history: Dict[str, List[Dict[str, Any]]] = dict(
        session.project_failure_history or {}
    )
    # Copy each entry into a NEW dict. Mutating the entries in place would
    # also mutate SQLAlchemy's committed-state snapshot of this JSONB column
    # (they share objects), so the reassigned value would compare equal to
    # the snapshot and the change would never be flushed. Fresh dicts make
    # the difference structural and detectable.
    entries = [dict(e) for e in (history.get(task_key) or [])]
    for entry in reversed(entries):
        if entry.get("status") == "dispatched":
            entry["reason"] = reason
            entry["status"] = status
            entry["failed_at"] = at
            if approach and not entry.get("approach"):
                entry["approach"] = approach
            history[task_key] = entries
            session.project_failure_history = history
            return
    record_failure(session, task_key, approach=approach or "unknown",
                   rung="retry", reason=reason, at=at)
