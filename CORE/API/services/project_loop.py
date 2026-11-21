"""project-agent §6.1 — the loop that actually moves a project forward.

One tick: check the project may act at all, then advance every task that
is ready and not parked. Everything it decides comes from modules that
already exist and are tested on their own —

    project_plan        what is ready, and whether the plan is valid
    project_escalation  what to do next for a task, and what bounds it
    project_dispatch    who it acts as, and what each wizard is told
    project_events      what gets written down, and what it cost

— so this file is deliberately thin. It sequences, it does not decide.

A tick is idempotent in the sense that matters: calling it twice in quick
succession advances nothing twice, because a task that has just been
dispatched is not stalled and `plan_next_attempt` returns "wait".
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def project_conversation_enabled() -> bool:
    """conversational-project-builder §1.5 — master feature flag.

    Gates ALL conversational-project-builder behaviour (the man-in-the-middle
    align→checklist→checkpoint loop, intent classification, and the new
    project gate state). Default OFF: the legacy one-shot project path is
    completely unchanged while this is false. Additive + flag-gated so the
    two builders never diverge (design "Risks / Trade-offs").
    """
    return os.getenv("PROJECT_CONVERSATION_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _completed_tasks(session) -> List[str]:
    done = []
    for artifact in (session.project_artifacts or []):
        key = artifact.get("task_key") if isinstance(artifact, dict) else None
        if key:
            done.append(key)
    return done


def _last_event_age_s(db, session, task_key: str) -> Optional[float]:
    """How long since anything happened on THIS task.

    Strictly per-task, and that is load-bearing: a project runs many tasks
    at once, and an earlier version keyed on the project's most recent event
    across every run. That made one task's activity look like every task's,
    so a busy project's fresh, never-dispatched tasks were read as "in
    flight, still progressing" and never got dispatched — and once the wait
    guard stopped keying on the attempt count, that would have stalled the
    whole plan.

    So a task has an age only when it has an in-flight attempt of its own. If
    that attempt is linked to a WorkflowRun, its newest run event is the age
    — escalation shares one notion of "stuck" with the build pipeline rather
    than inventing a second. Before any event lands (the window between
    handing work off and the first event) the dispatch stamp's age stands
    in, so a task that is genuinely running is not read as "nothing ran".

    None means nothing is in flight for this task — it is dispatchable.
    """
    import models
    from services.project_escalation import in_flight_age_s, in_flight_run_id

    flight = in_flight_age_s(session, task_key)
    if flight is None:
        return None

    run_id = in_flight_run_id(session, task_key)
    if run_id is not None:
        ev = (
            db.query(models.WorkflowRunEvent)
            .filter(models.WorkflowRunEvent.workflow_run_id == run_id)
            .order_by(models.WorkflowRunEvent.id.desc())
            .first()
        )
        if ev is not None and ev.ts is not None:
            ts = ev.ts if ev.ts.tzinfo else ev.ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds()

    return flight


def tick(db, session, *, max_dispatches: int = 3) -> Dict[str, Any]:
    """Advance a project by one step. Returns what it did and why.

    `max_dispatches` bounds one tick, not the project. Without it a plan
    with forty ready tasks would fan out forty builds in a single call and
    the spend ledger would only be able to report it afterwards.

    conversational-project-builder §3.1 — when the flag is on AND this project
    has a conversational gate (``project_gate_state`` set by the align/confirm
    step), the CHECKLIST supersedes ``project_plan`` as the dispatch source:
    the loop walks ``ProjectChecklistTask`` rows in order, builds ONE unit,
    then halts at the checkpoint until the user approves. A legacy one-shot
    project (no gate) — or the flag being off — keeps the autonomous plan
    dispatch below completely unchanged.
    """
    if project_conversation_enabled() and session.project_gate_state:
        from utils.paid_import import optional_paid

        # project-checkpoint is a Paid module: services/project_checkpoint.py is
        # denied by open-core/manifest.yaml (it pulls project_conversation +
        # wizard_queue pieces, both paid), so on CE the carve strips it and the
        # module is absent at call time. Guarded (optional_paid) rather than raw:
        # the checklist tick is the conversational-gate path, so on CE it DEGRADES
        # to the autonomous plan dispatch below — the exact behavior a project
        # with no gate already takes — rather than ImportError-500 taking the
        # whole tick out. On the paid tree `pcp` is the module itself and this
        # path is byte-for-byte the existing one.
        pcp = optional_paid("services.project_checkpoint")
        if pcp is not None:
            return pcp.checklist_tick(db, session)

    import models
    from services import project_dispatch as pd
    from services import project_escalation as esc
    from services import project_events as pe
    from services import project_plan as pp
    from services.project_runs import project_runs

    outcome: Dict[str, Any] = {
        "project_session_id": session.id,
        "dispatched": [],
        "escalated": [],
        "parked": [],
        "waiting": [],
        "halted": [],
    }

    # §9.4/§9.5 — done means an output the user accepted. Once accepted the
    # project stops dispatching entirely: there is nothing left to build, and
    # a never-stop loop that kept spending after acceptance would be a bug,
    # not persistence.
    if pp.is_complete(session):
        outcome["blocked"] = "complete"
        outcome["reason"] = (
            "This project has been accepted and is complete; no further work "
            "is dispatched."
        )
        return outcome

    # §6.1b — an agent working on stale authority is worse than one that
    # stops and says why.
    try:
        pd.assert_can_dispatch(session)
    except pd.ProjectCredentialsLapsed as exc:
        outcome["blocked"] = "credentials_lapsed"
        outcome["reason"] = str(exc)
        return outcome

    # §5.3 — nothing gets built before the project says what finished
    # looks like, because every test downstream would be synthetic.
    try:
        pp.assert_ready_to_build(session)
    except pp.ProjectNotReady as exc:
        outcome["blocked"] = "no_criteria"
        outcome["reason"] = str(exc)
        return outcome

    plan = session.project_plan
    if not plan:
        outcome["blocked"] = "no_plan"
        outcome["reason"] = "This project has no plan yet."
        return outcome
    try:
        pp.validate(plan)
    except pp.PlanInvalid as exc:
        outcome["blocked"] = "invalid_plan"
        outcome["reason"] = str(exc)
        return outcome

    ready = esc.unparked_work(session, completed=_completed_tasks(session))
    if not ready:
        outcome["blocked"] = "nothing_ready"
        outcome["reason"] = (
            "Every task is either done, parked awaiting an answer, or "
            "waiting on one that is."
        )
        return outcome

    runs = project_runs(db, session.id)
    run_id = runs[-1].id if runs else None
    tasks_by_key = {t["key"]: t for t in plan.get("tasks", [])
                    if isinstance(t, dict) and t.get("key")}

    for task_key in ready[:max_dispatches]:
        decision = esc.plan_next_attempt(
            db, session, task_key,
            tenant_id=session.tenant_id,
            last_event_age_s=_last_event_age_s(db, session, task_key),
        )
        action = decision["action"]

        if action == "wait":
            outcome["waiting"].append(task_key)
            continue

        if action == "halt":
            outcome["halted"].append(decision)
            if run_id:
                pe.record(db, workflow_run_id=run_id, verb=pe.VERB_PARKED,
                          level="warn", task_key=task_key,
                          message=f"{task_key}: {decision['reason']}")
            continue

        if action == "park":
            question = (
                f"I've tried everything I know for '{task_key}' and it still "
                f"isn't working. Approaches so far: "
                f"{', '.join(decision['tried']) or 'none recorded'}. "
                f"How would you tackle it?"
            )
            esc.park(session, task_key, question=question, at=_now_iso())
            # §7.1/§7.2 — reach the user in-product and by email, naming the
            # project and the subject. Records a durable handoff so an answer
            # arriving arbitrarily later finds the parked work. Suppressed
            # (returns None) if a question for this task is already
            # outstanding (§7.3).
            handoff = esc.raise_question(
                db, session, task_key,
                question=question,
                tried=decision.get("tried") or [],
                rung=esc.RUNG_ASK,
                at=_now_iso(),
            )
            outcome["parked"].append({
                "task_key": task_key,
                "question": question,
                "handoff_id": handoff.id if handoff is not None else None,
            })
            if run_id:
                pe.record(db, workflow_run_id=run_id, verb=pe.VERB_PARKED,
                          task_key=task_key,
                          message=f"parked {task_key} after "
                                  f"{decision['attempts']} attempts")
                if handoff is not None:
                    pe.record(db, workflow_run_id=run_id, verb=pe.VERB_ASKED,
                              task_key=task_key, message=question)
            continue

        # action == "escalate": dispatch an attempt.
        task = tasks_by_key.get(task_key) or {"key": task_key}
        guidance = decision.get("guidance")  # §7.4 — a human answer, if any
        resumed = decision["rung"] == esc.RUNG_HUMAN_GUIDED
        child = pd.open_child_session(
            db, session, "wizard",
            title=f"{task.get('label') or task_key} — {decision['rung']}",
        )
        payload = pd.node_wizard_payload(
            task, session.project_criteria, avoid=decision.get("avoid"),
            guidance=guidance)

        # §7.4 — this answer has now been tried, so it unblocks the task once
        # and not forever: a human-guided attempt that also fails parks again
        # with a fresh question rather than looping on the same reply.
        if resumed:
            esc.consume_answer(session, task_key)

        # Record the attempt at the moment of dispatch, at the rung it went
        # out on. This is half of the idempotency fix (the other half is the
        # in-flight wait guard): without it the attempt count stayed 0 and
        # the ladder never advanced, so a re-dispatch repeated the same rung
        # forever. Written before mark_in_flight so the history reflects the
        # dispatch even if the in-flight stamp is later cleared. The consumer
        # settles this same entry with the outcome (`settle_attempt`).
        from services import project_context as pc
        pc.record_attempt(session, task_key, rung=decision["rung"],
                          at=_now_iso())

        # Mark it running BEFORE the event, so a tick that races another
        # sees it in flight rather than dispatching a second attempt.
        esc.mark_in_flight(session, task_key, at=_now_iso(),
                           child_session_id=child.id)

        # §6.1 — consume the node_wizard_payload into a durable dispatch
        # record instead of dropping it when the tick returns. The build the
        # payload describes is run by the owner-wired path; this is the row
        # it is run from and tracked against.
        dispatch = models.ProjectHandoff(
            project_session_id=session.id,
            tenant_id=session.tenant_id,
            task_key=task_key,
            kind="wizard_dispatch",
            status="dispatched",
            payload=payload,
            child_session_id=child.id,
            rung=decision["rung"],
        )
        db.add(dispatch)
        db.flush()

        record = {
            "task_key": task_key,
            "rung": decision["rung"],
            "child_session_id": child.id,
            "backoff_seconds": decision["backoff_seconds"],
            "handoff_id": dispatch.id,
            "payload": payload,
        }
        outcome["dispatched" if decision["attempts"] == 0 else "escalated"].append(record)

        if run_id:
            verb = (pe.VERB_DISPATCHED if decision["attempts"] == 0
                    else pe.VERB_ESCALATED)
            pe.record(
                db, workflow_run_id=run_id, verb=verb, task_key=task_key,
                message=(f"{task_key}: {decision['rung']} "
                         f"(attempt {decision['attempts'] + 1})"),
                extra={"child_session_id": child.id,
                       "rung": decision["rung"]},
            )
            # §7.4 — a human-guided attempt IS the parked task resuming, and
            # that is an action worth a verb of its own in the ledger.
            if resumed:
                pe.record(
                    db, workflow_run_id=run_id, verb=pe.VERB_RESUMED,
                    task_key=task_key,
                    message=f"{task_key}: resumed with the user's answer",
                    extra={"child_session_id": child.id},
                )

    db.commit()
    return outcome
