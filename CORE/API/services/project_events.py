"""project-agent §4 — what the agent writes down, and what it costs.

`WorkflowRunEvent` is retained alongside its run and read live by the
run-detail Logs panel while people debug. An agent narrating its reasoning
turn by turn would drown the orchestrator and traffic-cop lines that make
that panel useful — the very lines someone opened it for.

So the event log gets VERBS: what the agent did. Reasoning stays in the
project session transcript, one level up and separately readable. That is
design D9, and `assert_is_action` below is what enforces it rather than a
line in a prompt asking the model to be brief.

Cost rides in the existing `event_metadata` JSONB rather than becoming a
second surface. What a step did and what it cost are then read together,
on one timeline, and a project's spend is derived by summing the timeline
that already exists — no new table to keep in step with reality.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# The agent's source tag in workflow_run_events.source, alongside the
# existing 'orchestrator' / 'traffic_cop' / 'worker'.
SOURCE_AGENT = "agent"

# §4.1 — the complete vocabulary. Each is something the agent DID and a
# reader can act on. There is deliberately no verb for considering,
# planning, reasoning or deciding: those happen in the transcript.
VERB_DISPATCHED = "dispatched"   # handed work to a wizard or a workflow
VERB_ESCALATED = "escalated"     # moved up a rung; approach changed
VERB_PARKED = "parked"           # cannot proceed; question raised, others continue
VERB_ASKED = "asked"             # question delivered to the user
VERB_RESUMED = "resumed"         # an answer arrived and unblocked a parked task
VERB_PUBLISHED = "published"     # an artifact became usable
VERB_ACCEPTED = "accepted"       # the user accepted the result; project done

VERBS = (
    VERB_DISPATCHED, VERB_ESCALATED, VERB_PARKED, VERB_ASKED,
    VERB_RESUMED, VERB_PUBLISHED, VERB_ACCEPTED,
)

# Words that mean the agent is narrating rather than reporting. Checked
# against the VERB, not the message — a message may legitimately quote a
# user or explain a failure.
_NARRATION = frozenset({
    "thinking", "considering", "planning", "reasoning", "deciding",
    "evaluating", "wondering", "noting", "observing", "analysing",
    "analyzing", "reflecting",
})


class NotAnAction(ValueError):
    """Raised when something tries to log deliberation as an event."""


def assert_is_action(verb: str) -> None:
    """§4.2 — refuse to write deliberation into the run event log.

    Enforced in code because the alternative is a prompt instruction, and
    prompt instructions degrade under load, across model changes, and at
    the end of long contexts. The panel this protects is read live during
    debugging; filling it with an agent's internal monologue costs
    somebody an outage.
    """
    if verb in _NARRATION:
        raise NotAnAction(
            f"{verb!r} is deliberation, not an action. The run event log "
            "records what the agent DID; reasoning belongs in the project "
            "session transcript."
        )
    if verb not in VERBS:
        raise NotAnAction(
            f"{verb!r} is not an agent event verb. Use one of: "
            f"{', '.join(VERBS)}."
        )


def record(
    db,
    *,
    workflow_run_id: int,
    verb: str,
    message: str,
    level: str = "info",
    task_key: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    credits: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Write one agent action to the run event log, with what it cost.

    Best-effort in the same way the orchestrator's emitter is: a full or
    missing event log must never break a run. `NotAnAction` is the one
    exception that propagates — that is a programming error, and swallowing
    it would let deliberation leak in silently, which is the whole thing
    §4.2 exists to prevent.
    """
    assert_is_action(verb)

    metadata: Dict[str, Any] = {
        "agent_verb": verb,
        "tokens": {
            "prompt": int(prompt_tokens),
            "completion": int(completion_tokens),
            "total": int(prompt_tokens) + int(completion_tokens),
        },
    }
    if task_key:
        metadata["task_key"] = task_key
    if credits is not None:
        metadata["credits"] = float(credits)
    if extra:
        metadata.update(extra)

    try:
        from models import WorkflowRunEvent
        db.add(WorkflowRunEvent(
            workflow_run_id=workflow_run_id,
            source=SOURCE_AGENT,
            level=level,
            message=message,
            event_metadata=metadata,
        ))
        # Not committed here, matching workflow_orchestrator._emit_event:
        # if the caller rolls back, the event rolls back with it, because
        # an event about work that did not happen is worse than no event.
    except Exception as exc:
        logger.debug("agent event write failed (non-fatal): %s", exc)


def _event_cost(event) -> Dict[str, Any]:
    meta = event.event_metadata or {}
    tokens = meta.get("tokens") or {}
    return {
        "task_key": meta.get("task_key"),
        "verb": meta.get("agent_verb"),
        "total_tokens": int(tokens.get("total") or 0),
        "credits": float(meta.get("credits") or 0.0),
    }


def project_spend(db, project_session_id: str) -> Dict[str, Any]:
    """§4.3 — what a project has consumed, and which task ate it.

    Derived by summing the event timeline rather than from a separate
    ledger table. One surface cannot drift from the other when there is
    only one surface.

    `by_task` is the number that matters: "this node has consumed 40% of
    the project across 19 attempts" is what makes a never-stop loop
    visible before it is a surprise, and a project-wide total alone
    cannot say it.
    """
    import models
    from services.project_runs import project_runs

    runs = project_runs(db, project_session_id)
    if not runs:
        return {
            "project_session_id": project_session_id,
            "total_tokens": 0, "total_credits": 0.0,
            "by_task": {}, "event_count": 0, "runs": 0,
        }

    events = (
        db.query(models.WorkflowRunEvent)
        .filter(
            models.WorkflowRunEvent.workflow_run_id.in_([r.id for r in runs]),
            models.WorkflowRunEvent.source == SOURCE_AGENT,
        )
        .all()
    )

    by_task: Dict[str, Dict[str, Any]] = {}
    total_tokens = 0
    total_credits = 0.0
    for ev in events:
        cost = _event_cost(ev)
        total_tokens += cost["total_tokens"]
        total_credits += cost["credits"]
        key = cost["task_key"] or "_unattributed"
        bucket = by_task.setdefault(key, {
            "tokens": 0, "credits": 0.0, "events": 0, "attempts": 0,
        })
        bucket["tokens"] += cost["total_tokens"]
        bucket["credits"] += cost["credits"]
        bucket["events"] += 1
        if cost["verb"] in (VERB_DISPATCHED, VERB_ESCALATED):
            bucket["attempts"] += 1

    for key, bucket in by_task.items():
        bucket["share_of_tokens"] = (
            round(bucket["tokens"] / total_tokens, 4) if total_tokens else 0.0
        )

    return {
        "project_session_id": project_session_id,
        "total_tokens": total_tokens,
        "total_credits": round(total_credits, 6),
        "by_task": by_task,
        "event_count": len(events),
        "runs": len(runs),
    }


def costliest_task(db, project_session_id: str) -> Optional[Dict[str, Any]]:
    """The task consuming the largest share, if any work has been recorded.

    Separate from project_spend because this is the question a human asks
    — "what is eating my budget" — and answering it should not require
    reading a dict and sorting it yourself.
    """
    spend = project_spend(db, project_session_id)
    if not spend["by_task"]:
        return None
    key, bucket = max(spend["by_task"].items(), key=lambda kv: kv[1]["tokens"])
    return {"task_key": key, **bucket}


def agent_timeline(db, workflow_run_id: int) -> List[Dict[str, Any]]:
    """The agent's actions on one run, oldest first, with their costs."""
    import models
    events = (
        db.query(models.WorkflowRunEvent)
        .filter(
            models.WorkflowRunEvent.workflow_run_id == workflow_run_id,
            models.WorkflowRunEvent.source == SOURCE_AGENT,
        )
        .order_by(models.WorkflowRunEvent.id.asc())
        .all()
    )
    return [
        {
            "id": ev.id,
            "level": ev.level,
            "message": ev.message,
            "ts": ev.ts.isoformat() if ev.ts else None,
            **_event_cost(ev),
        }
        for ev in events
    ]
