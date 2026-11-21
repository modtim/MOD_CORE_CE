"""project-agent §6.3–§6.6 — never stop, never block, never run away.

The agent does not give up on a task. It also does not repeat itself, wait
on a question, or spend without a meter. Those four together are what make
"never stop" a feature rather than a runaway loop.

THE LADDER (§6.3). Each rung changes something REAL. A retry counter is the
wrong primitive because attempt six is not different from attempt five if
it is the same attempt:

    0 retry          transient — registry, network, a flaky dependency
    1 recipe         different base image, different library, pinned
                     rather than latest
    2 decomposition  split the task, or compose existing artifacts instead
                     of building a new one
    3 ask            put the question to the user, and keep working on
                     everything else

Rung 2 is the one a retry loop can never reach, and it is where "build a
node that does photogrammetry" actually gets fixed — upstream, in the plan
the agent is holding.

WHAT TRIGGERS IT (§6.4). Lack of progress, not an attempt count. A build
producing activity is a build that is working, however slowly; a build that
has recorded nothing for N minutes is stuck. `node_builder.eta.detect_stall`
already computes exactly that from event age and is already surfaced as
`stall_reason`, so escalation reuses it rather than growing a second notion
of "stuck".

PARKING (§6.5). Reaching the top of the ladder parks the task with its
question and its history. Every other task in the plan continues. An agent
that blocks on its first question is an agent that stops, so asking has to
be asynchronous or never-stop and ask-for-help cannot coexist.

WHAT BOUNDS IT (§6.6). Not an attempt limit — an arbitrary number either
stops too early on a hard problem or too late on an impossible one. The
tenant's credit balance is a real economic signal, and backoff makes a
parked task cost pennies over days rather than dollars an hour.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# §6.3 — the ladder. Order matters; index is the rung.
RUNG_RETRY = "retry"
RUNG_RECIPE = "recipe"
RUNG_DECOMPOSITION = "decomposition"
RUNG_ASK = "ask"

LADDER: Tuple[str, ...] = (RUNG_RETRY, RUNG_RECIPE, RUNG_DECOMPOSITION, RUNG_ASK)

# §7.4 — not a rung on the automatic ladder, but the attempt an ANSWER
# unblocks. When a parked task's question is answered, the task is not
# repeating a rung it already climbed — it is trying the human's approach,
# which is new. So a task whose ladder is exhausted becomes dispatchable
# again exactly once per unconsumed answer, at this rung.
RUNG_HUMAN_GUIDED = "human_guided"

# §6.4 — how long a task may record nothing before it counts as stuck.
# Matches detect_stall's own default; stated here so the two cannot drift
# apart silently.
STALL_THRESHOLD_S = 5 * 60

# §6.6 — backoff. Doubling from a minute, capped at six hours.
#
# The cap matters as much as the growth: without one, attempt 20 lands
# somewhere past the heat death of the project and a task the user could
# have unblocked in a day is silently abandoned. With it, a parked task
# retries four times a day forever, which is cheap enough to run
# indefinitely and frequent enough to notice a fixed dependency.
BACKOFF_BASE_S = 60
BACKOFF_CAP_S = 6 * 60 * 60


class BudgetExhausted(Exception):
    """The tenant cannot pay for another attempt.

    Distinct from failure on purpose: the work did not fail, it stopped,
    and telling someone their build broke when they actually ran out of
    credit sends them to debug the wrong thing.
    """


def backoff_seconds(attempt: int) -> int:
    """Delay before attempt N. attempt is 1-based; attempt 1 waits 0."""
    if attempt <= 1:
        return 0
    delay = BACKOFF_BASE_S * (2 ** (attempt - 2))
    return int(min(delay, BACKOFF_CAP_S))


def next_rung(tried_rungs: List[str]) -> Optional[str]:
    """The next rung that changes something, given what has been tried.

    Returns None only when 'ask' itself has been used — at which point the
    task parks. It never returns a rung already climbed, because repeating
    a rung is repeating an attempt.
    """
    for rung in LADDER:
        if rung not in tried_rungs:
            return rung
    return None


def should_escalate(
    *,
    run_status: str,
    last_event_age_s: Optional[float],
    threshold_s: int = STALL_THRESHOLD_S,
) -> Optional[str]:
    """§6.4 — whether this task has stopped making progress.

    Delegates to the existing stall detector rather than reimplementing
    "stuck". Returns the stall reason, or None when the task is either
    progressing or not running.
    """
    from node_builder.eta import detect_stall
    return detect_stall(run_status, last_event_age_s, threshold_s=threshold_s)


def can_afford_another_attempt(db, tenant_id: int) -> bool:
    """§6.6 — whether the tenant can pay for another go.

    Mirrors `llm_charging.preflight`'s permissiveness deliberately: it
    refuses only a tenant with no balance AND no way to cover an advance.
    A balance lookup that FAILS returns True — failing closed would turn a
    billing-system wobble into every project on the platform stopping, and
    an agent that halts because a lookup timed out is worse than one that
    takes one more attempt it might not be able to pay for.
    """
    try:
        from utils.credit_manager import CreditManager
        balance = CreditManager.get_credit_balance(db, tenant_id) or {}
    except Exception:  # noqa: BLE001
        logger.debug("escalation: balance lookup failed; allowing the attempt",
                     exc_info=True)
        return True

    if balance.get("unlimited") or balance.get("can_advance"):
        return True
    remaining = balance.get("credits_remaining")
    # OWNER-DECISIONS 09-29 — check plan + purchased pools combined; a
    # tenant with only purchased (top-up) credits left can still afford it.
    if remaining is not None:
        remaining = remaining + int(balance.get("purchased_credits_remaining") or 0)
    return remaining is None or remaining > 0


def plan_next_attempt(
    db,
    session,
    task_key: str,
    *,
    tenant_id: int,
    run_status: str = "RUNNING",
    last_event_age_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Decide what happens next for one task. Does not act.

    Returns a decision dict with an ``action`` of:

      wait       progressing, or the backoff window has not elapsed
      escalate   climb to ``rung`` and try again
      park       the ladder is exhausted; raise the question, keep the rest
                 of the plan moving
      halt       the tenant cannot pay; stopped, not failed

    Kept as a pure decision so the loop that acts on it is testable
    without dispatching anything, and so an operator can ask "what would
    it do next" without it doing it.
    """
    from services.project_context import tried_approaches

    history = (session.project_failure_history or {}).get(task_key) or []
    attempts = len(history)
    tried_rungs = [e.get("rung") for e in history if e.get("rung")]

    if attempts and not can_afford_another_attempt(db, tenant_id):
        return {
            "action": "halt",
            "task_key": task_key,
            "attempts": attempts,
            "reason": (
                "Out of credits. This task stopped rather than failed — the "
                "last attempt did not break, there was nothing left to pay "
                "for the next one."
            ),
        }

    stall = should_escalate(run_status=run_status,
                            last_event_age_s=last_event_age_s)

    # "wait" means something is IN FLIGHT and making progress. It must not
    # mean "we have no information": detect_stall returns None both for a
    # build that just logged and for a task with no run at all, and
    # conflating those leaves a failed task waiting forever on activity
    # that will never come, because nothing is running to produce it.
    #
    # Idempotency (the reason this does NOT gate on `attempts`): a task that
    # was just dispatched is in flight and not yet stalled REGARDLESS of how
    # many times it has failed before — including a first attempt, whose
    # `attempts` is 0. Gating the wait on `attempts` meant a freshly
    # dispatched task looked identical to one that had never run, so the
    # next tick dispatched it a second time and the one after that a third.
    # In flight and not stalled always waits; the attempt is recorded at
    # dispatch (see the loop) so the ladder still advances across ticks.
    in_flight = last_event_age_s is not None
    if in_flight and stall is None:
        return {
            "action": "wait",
            "task_key": task_key,
            "attempts": attempts,
            "reason": "still making progress",
        }

    rung = next_rung(tried_rungs)
    if rung is None:
        # §7.4 — the automatic ladder is spent, but an answer to the parked
        # question is a NEW approach the ladder could never reach. Resume the
        # task at the human-guided rung, once, carrying the answer into it.
        guidance = pending_answer(session, task_key)
        if guidance is not None:
            return {
                "action": "escalate",
                "task_key": task_key,
                "rung": RUNG_HUMAN_GUIDED,
                "attempts": attempts,
                "backoff_seconds": backoff_seconds(attempts + 1),
                "avoid": tried_approaches(session, task_key),
                "guidance": guidance,
                "stall_reason": stall,
            }
        return {
            "action": "park",
            "task_key": task_key,
            "attempts": attempts,
            "tried": tried_approaches(session, task_key),
            "reason": (
                "Every approach on the ladder has been tried. Parked with a "
                "question; the rest of the plan continues."
            ),
        }

    return {
        "action": "escalate",
        "task_key": task_key,
        "rung": rung,
        "attempts": attempts,
        "backoff_seconds": backoff_seconds(attempts + 1),
        "avoid": tried_approaches(session, task_key),
        "stall_reason": stall,
    }


def park(session, task_key: str, *, question: str, at: str) -> Dict[str, Any]:
    """§6.5 — record a task as parked, without stopping the project.

    The question and the approaches already tried are kept together,
    because an answer arriving three weeks later is only useful next to
    the record of what it is answering.
    """
    from services.project_context import tried_approaches

    parked = dict((session.project_plan or {}).get("_parked") or {}) \
        if isinstance(session.project_plan, dict) else {}
    parked[task_key] = {
        "question": question,
        "parked_at": at,
        "tried": tried_approaches(session, task_key),
        "answered": False,
    }
    if isinstance(session.project_plan, dict):
        plan = dict(session.project_plan)
        plan["_parked"] = parked
        session.project_plan = plan
    return parked[task_key]


def mark_in_flight(session, task_key: str, *, at: str,
                   child_session_id: Optional[str] = None,
                   run_id: Optional[int] = None) -> None:
    """Record that an attempt is running for this task.

    Without this the loop cannot tell "dispatched a moment ago" from
    "nothing has ever run", so the next tick escalates work that has not
    had a chance to fail yet — burning a rung per tick and reaching the
    top of the ladder in seconds.

    ``run_id`` is the WorkflowRun the dispatch consumer created for this
    attempt, if any. It is what lets the stall check read THIS task's own
    run events rather than the project's most recent event across every
    task — so one task's activity cannot mask another's stall, and a fresh
    never-dispatched task (no in-flight entry at all) is never mistaken for
    one that is running. It is set once the run exists via `link_in_flight_run`.

    Cleared by `clear_in_flight` when the attempt reports back; until then
    its age is what the stall check reads when no run events exist yet.
    """
    if not isinstance(session.project_plan, dict):
        return
    plan = dict(session.project_plan)
    flying = dict(plan.get("_in_flight") or {})
    flying[task_key] = {"since": at, "child_session_id": child_session_id,
                        "run_id": run_id}
    plan["_in_flight"] = flying
    session.project_plan = plan


def link_in_flight_run(session, task_key: str, run_id: int) -> None:
    """Attach the WorkflowRun the consumer created to this task's in-flight
    record, so the stall check reads this task's own run events."""
    if not isinstance(session.project_plan, dict):
        return
    plan = dict(session.project_plan)
    flying = dict(plan.get("_in_flight") or {})
    entry = dict(flying.get(task_key) or {})
    if not entry:
        return
    entry["run_id"] = run_id
    flying[task_key] = entry
    plan["_in_flight"] = flying
    session.project_plan = plan


def in_flight_run_id(session, task_key: str) -> Optional[int]:
    """The WorkflowRun linked to this task's in-flight attempt, or None."""
    if not isinstance(session.project_plan, dict):
        return None
    entry = (session.project_plan.get("_in_flight") or {}).get(task_key)
    if not entry:
        return None
    return entry.get("run_id")


def clear_in_flight(session, task_key: str) -> None:
    if not isinstance(session.project_plan, dict):
        return
    plan = dict(session.project_plan)
    flying = dict(plan.get("_in_flight") or {})
    if flying.pop(task_key, None) is not None:
        plan["_in_flight"] = flying
        session.project_plan = plan


def in_flight_age_s(session, task_key: str, *,
                    now: Optional[Any] = None) -> Optional[float]:
    """Seconds since an attempt was dispatched for this task, or None."""
    from datetime import datetime, timezone
    if not isinstance(session.project_plan, dict):
        return None
    entry = (session.project_plan.get("_in_flight") or {}).get(task_key)
    if not entry or not entry.get("since"):
        return None
    try:
        started = datetime.fromisoformat(entry["since"])
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - started).total_seconds()


def parked_tasks(session) -> Dict[str, Any]:
    if not isinstance(session.project_plan, dict):
        return {}
    return {k: v for k, v in (session.project_plan.get("_parked") or {}).items()
            if not v.get("answered")}


def unparked_work(session, completed: List[str]) -> List[str]:
    """§6.5 — what the agent should be doing while a task is parked.

    This is the whole of park-not-block: a task waiting on a human must
    not hold up work that never depended on it.
    """
    from services.project_plan import ready_tasks
    blocked = set(parked_tasks(session))
    return [k for k in ready_tasks(session.project_plan or {}, completed)
            if k not in blocked]


def resume(session, task_key: str, *, answer: str, at: str) -> bool:
    """An answer arrived. Unpark, keeping everything already recorded.

    Returns False when the task was not parked, so a duplicate answer is
    a no-op rather than an error — a user replying twice to the same email
    should not break their project.

    The answer is left ``consumed=False`` so `pending_answer` reports it and
    the next tick resumes the task at the human-guided rung (§7.4). Nothing
    already tried and no completed work is touched — the failure history is
    durable (D2) and the answer resumes the work rather than restarting it.
    """
    if not isinstance(session.project_plan, dict):
        return False
    parked = dict((session.project_plan.get("_parked") or {}))
    entry = parked.get(task_key)
    if not entry or entry.get("answered"):
        return False
    entry = dict(entry)
    entry.update({"answered": True, "answer": answer, "answered_at": at,
                  "consumed": False})
    parked[task_key] = entry
    plan = dict(session.project_plan)
    plan["_parked"] = parked
    session.project_plan = plan
    return True


def pending_answer(session, task_key: str) -> Optional[str]:
    """§7.4 — an answered-but-not-yet-tried answer for this task, or None.

    "Not yet tried" is what stops a single answer from unblocking the task
    forever: once the human-guided attempt is dispatched the answer is
    consumed, so a task whose human-guided attempt also fails parks again
    with a fresh question rather than looping on the same reply.
    """
    if not isinstance(session.project_plan, dict):
        return None
    entry = (session.project_plan.get("_parked") or {}).get(task_key)
    if not entry or not entry.get("answered") or entry.get("consumed"):
        return None
    return entry.get("answer")


def consume_answer(session, task_key: str) -> None:
    """Mark a resumed answer as tried, so it unblocks the task exactly once."""
    if not isinstance(session.project_plan, dict):
        return
    parked = dict((session.project_plan.get("_parked") or {}))
    entry = parked.get(task_key)
    if not entry or entry.get("consumed"):
        return
    entry = dict(entry)
    entry["consumed"] = True
    parked[task_key] = entry
    plan = dict(session.project_plan)
    plan["_parked"] = parked
    session.project_plan = plan


# --- §7 human handoff: raising a question and reintegrating its answer ------

def default_consequences(task_key: str) -> Dict[str, str]:
    """§7.2 — what the agent will do with each possible answer.

    A question that does not say what happens when you answer it is a
    question a busy user ignores. This states the two outcomes plainly and
    is stored structured so a surface can render it however it likes.
    """
    return {
        "if_you_answer": (
            "I'll try your approach on the next attempt for this task, "
            "keeping everything already built."
        ),
        "if_you_do_nothing": (
            "This task stays parked while the rest of the project keeps "
            "moving. Nothing already built is lost, and you can answer at "
            "any time — an hour or a month from now — and I'll pick it up."
        ),
    }


def _question_subject(session, task_key: str) -> str:
    """§7.1 — names the project and the subject, so it is answerable without
    opening every project."""
    project = (session.title or "your project").strip()
    return f"{project}: I need your input on '{task_key}'"


def _question_body(question: str, consequences: Dict[str, str]) -> str:
    lines = [question, ""]
    if consequences.get("if_you_answer"):
        lines.append(f"If you answer: {consequences['if_you_answer']}")
    if consequences.get("if_you_do_nothing"):
        lines.append(f"If you do nothing: {consequences['if_you_do_nothing']}")
    return "\n".join(lines)


def question_already_pending(db, session, task_key: str) -> bool:
    """§7.3 — a task already carrying an outstanding question does not raise
    a second. The information is already on its way to the user; asking again
    is noise, and a duplicate row would fork the answer's landing place.
    """
    import models
    return (
        db.query(models.ProjectHandoff)
        .filter(
            models.ProjectHandoff.project_session_id == session.id,
            models.ProjectHandoff.task_key == task_key,
            models.ProjectHandoff.kind == "question",
            models.ProjectHandoff.status == "outstanding",
        )
        .first()
        is not None
    )


def raise_question(db, session, task_key: str, *, question: str,
                   tried: Optional[List[str]] = None, rung: Optional[str] = None,
                   consequences: Optional[Dict[str, str]] = None,
                   at: Optional[str] = None):
    """§7.1/§7.2 — put a question to the project's user without blocking.

    Records a durable handoff row (so a late answer finds the parked work),
    raises a `Notification` for the project's user, and sends an email —
    both naming the project and the subject so it is answerable without
    opening every project. Everything past the DB rows is best-effort: a
    notification or email that fails must never stop the loop, which is
    still dispatching every other task.

    Returns the handoff row, or None when a question is suppressed (§7.3).
    """
    import models

    if question_already_pending(db, session, task_key):
        return None

    consequences = consequences or default_consequences(task_key)
    subject = _question_subject(session, task_key)
    body = _question_body(question, consequences)

    handoff = models.ProjectHandoff(
        project_session_id=session.id,
        tenant_id=session.tenant_id,
        task_key=task_key,
        kind="question",
        status="outstanding",
        question=question,
        consequences=consequences,
        tried=list(tried or []),
        rung=rung,
    )
    db.add(handoff)
    db.flush()

    notif = models.Notification(
        recipient_user_id=session.user_id,
        message=f"{subject}\n\n{body}",
        notification_type="project_question",
        is_read=False,
    )
    db.add(notif)
    db.flush()
    handoff.notification_id = notif.id

    _publish_notification(session.user_id)
    _send_question_email(db, session, subject, body)
    return handoff


def _publish_notification(user_id: int) -> None:
    try:
        from routers.notifications import publish_notification_event
        publish_notification_event(user_id)
    except Exception:  # noqa: BLE001
        logger.debug("project question realtime publish failed", exc_info=True)


def _send_question_email(db, session, subject: str, body: str) -> None:
    """§7.1 — reach the user by email as well as in-product.

    Best-effort by design: a missing template or an SMTP wobble must not
    stop a never-stop loop. The user still has the in-product notification,
    and the durable handoff row keeps the question answerable regardless.
    """
    import models
    try:
        user = (db.query(models.User)
                .filter(models.User.id == session.user_id).first())
        if user is None or not user.email:
            return
        from utils import email_sender
        email_sender.send_templated_email(
            db,
            session.tenant_id,
            "project_question",
            {
                "PROJECT_TITLE": session.title or "your project",
                "SUBJECT": subject,
                "QUESTION": body,
                "PROJECT_SESSION_ID": session.id,
            },
            user.email,
            recipient_name=user.username or "",
            subject=subject,
        )
    except Exception:  # noqa: BLE001
        logger.debug("project question email failed (non-fatal)", exc_info=True)


def outstanding_questions(db, session) -> List[Any]:
    """The unanswered questions raised for one project, oldest first."""
    import models
    return (
        db.query(models.ProjectHandoff)
        .filter(
            models.ProjectHandoff.project_session_id == session.id,
            models.ProjectHandoff.kind == "question",
            models.ProjectHandoff.status == "outstanding",
        )
        .order_by(models.ProjectHandoff.id.asc())
        .all()
    )


def answer_question(db, session, handoff, *, answer: str, at: str) -> bool:
    """§7.4 — reintegrate an answer arriving arbitrarily later.

    Marks the handoff answered and resumes the parked task with the answer
    applied, without repeating completed work. Returns False when the
    handoff was already answered, so a user replying twice is a harmless
    no-op rather than an error.
    """
    if handoff is None or handoff.status != "outstanding":
        return False

    handoff.answer = answer
    handoff.status = "answered"
    try:
        handoff.answered_at = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        handoff.answered_at = datetime.now(timezone.utc)

    # Resume is what makes the task dispatchable again; if it was not parked
    # (an answer to a question the loop already moved past) the handoff is
    # still recorded as answered so the reply is not lost.
    if handoff.task_key:
        resume(session, handoff.task_key, answer=answer, at=at)
    return True
