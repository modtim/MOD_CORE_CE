"""Service layer for the assistant/support-chat sub-module (Gate 2b
migration of ``routers/support_chat/assistant_support.py``).

Owns the ownership/kind policy the handlers in that module previously
inlined next to their ``db.query`` calls (404-vs-403-vs-409 checks), plus
the small amount of pure computation (usage-report aggregation, wizard-
metadata stamping) that rode along with those queries. See
``services/_base.py`` for the layering contract and the 404-vs-403 trap
this follows: CRUD fetches are UNSCOPED, and ownership is enforced here
so a legitimate 403 never collapses into a 404.

Everything else those handlers do — LLM dispatch, the tone-deescalation
pipeline, the wizard builder/critic pipeline, escalation email — stays in
the router (or ``_shared.py``, which this module does not touch); that is
orchestration, not data access, and is out of scope for this migration.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

import models
from crud import assistant_support as crud
from services._base import ConflictError, NotFoundError, ServiceError


class ForbiddenError(ServiceError):
    """Caller is authenticated but not entitled to the resource (router
    maps -> 403). ``services/_base.py`` ships ``NotFoundError`` /
    ``ConflictError`` but no forbidden error — declared here per
    migrate_rules #5."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


# ---------------------------------------------------------------------
# Session ownership
# ---------------------------------------------------------------------

def get_owned_session(
    db: Session, session_id: str, user_id: int, *, kind: Optional[str] = None,
) -> models.SupportChatSession:
    """Fetch a session the caller owns.

    Raises ``NotFoundError`` if the session doesn't exist, ``ForbiddenError``
    if it belongs to someone else, and (when ``kind`` is given) ``ConflictError``
    if it isn't a session of that kind — the same three-way check every
    session-scoped handler in this module did inline before the migration.
    """
    sess = crud.get_session_by_id(db, session_id)
    if sess is None:
        raise NotFoundError("Session not found")
    if sess.user_id != user_id:
        raise ForbiddenError("Not your session")
    if kind is not None and sess.kind != kind:
        raise ConflictError(f"Session is not a {kind} session")
    return sess


def get_session_messages(db: Session, session_id: str) -> List[models.SupportChatMessage]:
    return crud.list_session_messages(db, session_id)


def list_sessions(
    db: Session, user_id: int, *, kind: Optional[str], limit: int,
) -> List[models.SupportChatSession]:
    return crud.list_user_sessions(db, user_id, kind=kind, limit=limit)


# ---------------------------------------------------------------------
# Workflow-wizard test-run status
# ---------------------------------------------------------------------

def get_test_run_status(
    db: Session, run_id: int, user_id: int,
) -> Tuple[models.WorkflowRun, Optional[models.WizardTestRun]]:
    """Resolve a sandbox test-run's status. ``run not found`` -> 404,
    ``not your run`` (the run's workflow isn't owned by the caller) -> 403.
    """
    run = crud.get_workflow_run_by_id(db, run_id)
    if run is None:
        raise NotFoundError("run not found")

    wf = crud.get_workflow_by_id(db, run.workflow_id)
    if wf is None or wf.created_by_user_id != user_id:
        raise ForbiddenError("not your run")

    wtr = crud.get_wizard_test_run_by_run_id(db, run_id)
    return run, wtr


# ---------------------------------------------------------------------
# Quality-signal target resolution
# ---------------------------------------------------------------------

def get_quality_signal_target(
    db: Session, session_id: str, proposal_id: int, user_id: int,
) -> Tuple[models.SupportChatSession, models.SupportChatMessage]:
    """Resolve + validate the (session, proposal message) pair a
    quality-signal event refers to. Session ownership/kind checks run
    BEFORE the message lookup, matching the pre-migration order."""
    sess = get_owned_session(db, session_id, user_id, kind="wizard")
    msg = crud.get_message_by_id_in_session(db, proposal_id, sess.id)
    if msg is None:
        raise NotFoundError(f"proposal_id {proposal_id} not found in this session")
    return sess, msg


# ---------------------------------------------------------------------
# Queue-missing-nodes dispatch
# ---------------------------------------------------------------------

def get_queue_missing_session(
    db: Session, session_id: str, user_id: int,
) -> models.SupportChatSession:
    return get_owned_session(db, session_id, user_id, kind="wizard")


def stamp_task_wizard_metadata(
    db: Session, task_id: Any, metadata_updates: Dict[str, Any],
) -> None:
    """Merge the given keys into an already-enqueued task's payload — the
    read-modify-write the queue-missing-nodes flow uses to stamp
    ``wizard_metadata`` (+ trigger/auto-queue flags) after dispatch.
    No-op if the task row can't be found (matches pre-migration guard)."""
    task_row = crud.get_task_queue_row(db, task_id)
    if task_row is None:
        return
    payload = dict(task_row.task_payload or {})
    payload.update(metadata_updates)
    crud.update_task_payload(db, task_row, payload)


# ---------------------------------------------------------------------
# Email-conversation
# ---------------------------------------------------------------------

def get_session_for_email(
    db: Session, session_id: str, user_id: int,
) -> models.SupportChatSession:
    return get_owned_session(db, session_id, user_id)


# ---------------------------------------------------------------------
# Admin usage report
# ---------------------------------------------------------------------

def build_usage_report(db: Session, since_days: int) -> Dict[str, Any]:
    """Per-user / per-tenant message counts in the window — same
    aggregation the pre-migration inline handler did."""
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=since_days)
    rows = crud.usage_rows_since(db, cutoff)

    by_user: Dict[int, Dict[str, Any]] = {}
    by_tenant: Dict[int, int] = {}
    for user_id, tenant_id, kind in rows:
        slot = by_user.setdefault(user_id, {
            "user_id": user_id, "tenant_id": tenant_id,
            "general": 0, "logs": 0, "total": 0,
        })
        slot[kind] = slot.get(kind, 0) + 1
        slot["total"] += 1
        by_tenant[tenant_id] = by_tenant.get(tenant_id, 0) + 1

    return {
        "since_days": since_days,
        "users": sorted(by_user.values(), key=lambda r: -r["total"]),
        "tenants": [
            {"tenant_id": t, "messages": n}
            for t, n in sorted(by_tenant.items(), key=lambda x: -x[1])
        ],
    }
