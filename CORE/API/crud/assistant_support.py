"""CRUD layer for the assistant/support-chat sub-module (Gate 2b migration
of ``routers/support_chat/assistant_support.py`` — see services/_base.py
for the layering contract this follows).

The ONLY place ``db.query(models.X)`` lives for this slice of the
support_chat domain: general/logs chat history, session list/detail,
workflow-wizard test-run status / quality-signal / queue-missing-nodes
target resolution, email-conversation, and the admin usage report.

Every fetch-by-id here is intentionally caller-UNSCOPED (no ``user_id``
or ``tenant_id`` filter baked into the query) — the service layer
(``services/assistant_support_service.py``) applies ownership policy on
top, so a legitimate 403 never collapses into a 404 (migrate_rules #5).

``SupportChatSession`` / ``SupportChatMessage`` / ``WizardTestRun`` have
no ``deleted_at`` column (no soft-delete on this domain today).
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

import models


def get_session_by_id(db: Session, session_id: str) -> Optional[models.SupportChatSession]:
    """Fetch one support-chat session by id. Unscoped by design."""
    return (
        db.query(models.SupportChatSession)
          .filter(models.SupportChatSession.id == session_id)
          .first()
    )


def list_session_messages(db: Session, session_id: str) -> List[models.SupportChatMessage]:
    """All messages for a session, oldest first.

    Unbounded — matches pre-migration behavior. A session's message
    count is bounded by chat-turn count, not a general admin list, so
    this was never one of the audit's unbounded-list offenders.
    """
    return (
        db.query(models.SupportChatMessage)
          .filter(models.SupportChatMessage.session_id == session_id)
          .order_by(models.SupportChatMessage.id.asc())
          .all()
    )


def get_message_by_id_in_session(
    db: Session, message_id: int, session_id: str,
) -> Optional[models.SupportChatMessage]:
    """Fetch one message, scoped to a session (not to a user — the
    caller has already proven session ownership by this point)."""
    return (
        db.query(models.SupportChatMessage)
          .filter(models.SupportChatMessage.id == message_id,
                  models.SupportChatMessage.session_id == session_id)
          .first()
    )


def list_user_sessions(
    db: Session, user_id: int, *, kind: Optional[str], limit: int,
) -> List[models.SupportChatSession]:
    """Bounded list of a user's own sessions, most-recently-active first."""
    q = (
        db.query(models.SupportChatSession)
          .filter(models.SupportChatSession.user_id == user_id)
          .order_by(models.SupportChatSession.last_message_at.desc())
    )
    if kind in ("general", "logs", "wizard", "project"):
        q = q.filter(models.SupportChatSession.kind == kind)
    return q.limit(limit).all()


def get_workflow_run_by_id(db: Session, run_id: int) -> Optional[models.WorkflowRun]:
    return db.query(models.WorkflowRun).filter(models.WorkflowRun.id == run_id).first()


def get_workflow_by_id(db: Session, workflow_id: int) -> Optional[models.Workflow]:
    return db.query(models.Workflow).filter(models.Workflow.id == workflow_id).first()


def get_wizard_test_run_by_run_id(db: Session, run_id: int) -> Optional[models.WizardTestRun]:
    return (
        db.query(models.WizardTestRun)
          .filter(models.WizardTestRun.workflow_run_id == run_id)
          .first()
    )


def get_task_queue_row(db: Session, task_id: Any):
    """Fetch an ``ApiTaskQueue`` row by id. Imported lazily (function-local)
    to match the rest of the codebase's function-local-import convention
    for this model."""
    from models import ApiTaskQueue

    return db.query(ApiTaskQueue).filter(ApiTaskQueue.id == task_id).first()


def update_task_payload(db: Session, task_row: Any, payload: Dict[str, Any]) -> None:
    """Overwrite a task row's ``task_payload`` and commit — the
    read-modify-write pattern the queue-missing-nodes flow uses to stamp
    ``wizard_metadata`` onto an already-enqueued task."""
    task_row.task_payload = payload
    db.commit()


def usage_rows_since(
    db: Session, cutoff: _dt.datetime,
) -> Sequence[Tuple[int, int, str]]:
    """Per-message ``(user_id, tenant_id, kind)`` rows for the admin usage
    report — the join + filter exactly as the pre-migration inline query."""
    return (
        db.query(
            models.SupportChatSession.user_id,
            models.SupportChatSession.tenant_id,
            models.SupportChatSession.kind,
        )
          .join(
              models.SupportChatMessage,
              models.SupportChatMessage.session_id == models.SupportChatSession.id,
          )
          .filter(
              models.SupportChatMessage.created_at >= cutoff,
              models.SupportChatMessage.role == "user",
          )
          .all()
    )
