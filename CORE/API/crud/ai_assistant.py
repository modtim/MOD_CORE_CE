"""CRUD layer for the ai_assistant domain (Gate 2b migration of
``routers/ai_assistant.py`` — see ``services/_base.py`` for the layering
contract this follows).

The ONLY place ``db.query(models.X)`` lives for AI-assistant
conversations (``AIConversation``) and the ``WorkflowRun`` /
``ApiTaskQueue`` rows the one-shot DAG-analysis endpoint reads to build
its failed-task log summary.

Conversation fetches come in two flavours:
``get_conversation_scoped`` (tenant + owner filters IN THE QUERY — the
request-path read, "not yours" -> None -> caller 404) and
``get_conversation`` (unscoped, SECURITY-SENSITIVE — only the worker-facing
service surface, which the service layer scopes and audits). The old
service-layer-only ownership (``get_owned_conversation``) leaked a
cross-tenant 403 that doubled as an existence oracle; it is kept for
compatibility but no request-path caller uses it.

``AIConversation`` has a ``deleted_at`` column (soft-delete); the
analyze-dag temporary-conversation cleanup path uses a real
``db.delete`` (see ``hard_delete_conversation`` below) — that is
pre-existing behavior for throwaway rows, not the user-facing delete.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy.orm import Session

import models


def get_conversation(db: Session, conversation_id: Any) -> Optional[models.AIConversation]:
    """Fetch one conversation by its (UUID) conversation_id, excluding
    soft-deleted rows. UNSCOPED — SECURITY-SENSITIVE.

    This returns the row for ANY caller that can name a conversation_id,
    including one from another tenant. It exists only for the genuinely
    internal paths that have no user context of their own and authenticate
    some other way (the worker-facing ``/assistant/service/*`` endpoints,
    which the service layer scopes/audits — see
    ``services/ai_assistant_service.get_conversation_unscoped``).

    Every request-path (user-authenticated) caller MUST use
    ``get_conversation_scoped`` below instead: a cross-tenant or cross-user
    read of chat history is a data-isolation break (tenant-data-isolation,
    ISO 27001 AC-3)."""
    return (
        db.query(models.AIConversation)
        .filter(
            models.AIConversation.conversation_id == conversation_id,
            models.AIConversation.deleted_at.is_(None),
        )
        .first()
    )


def get_conversation_scoped(
    db: Session, conversation_id: Any, *,
    tenant_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Optional[models.AIConversation]:
    """The tenant- AND owner-scoped conversation read.

    Filters ``tenant_id`` AND ``user_id`` into the query itself, so a row
    that belongs to someone else never leaves the SQL, and returns ``None``
    for "not yours" — the caller answers 404, never 403, so the existence
    of other tenants' conversations is not revealed (the pre-migration
    403-vs-404 split in ``get_owned_conversation`` was an existence oracle).

    Soft-deleted rows are excluded (ISO 27001 no-hard-delete). Pass exactly
    one of the scopes only when the caller genuinely has no user context
    (a tenant-scoped service principal); a user-authenticated caller must
    pass BOTH."""
    q = db.query(models.AIConversation).filter(
        models.AIConversation.conversation_id == conversation_id,
        models.AIConversation.deleted_at.is_(None),
    )
    if tenant_id is not None:
        q = q.filter(models.AIConversation.tenant_id == tenant_id)
    if user_id is not None:
        q = q.filter(models.AIConversation.user_id == user_id)
    return q.first()


def list_conversations_for_user(
    db: Session, *, user_id: int, tenant_id: int, limit: int,
) -> List[models.AIConversation]:
    """Bounded list of a user's own conversations, most-recently-active
    first."""
    return (
        db.query(models.AIConversation)
        .filter(
            models.AIConversation.user_id == user_id,
            models.AIConversation.tenant_id == tenant_id,
            models.AIConversation.deleted_at.is_(None),
        )
        .order_by(models.AIConversation.last_activity.desc())
        .limit(limit)
        .all()
    )


def create_conversation(
    db: Session,
    *,
    conversation_id: Any,
    node_kind: Optional[str],
    orchestrator_run_id: Optional[str],
    user_id: int,
    tenant_id: int,
    assigned_worker_id: Optional[str],
    title: Optional[str],
) -> models.AIConversation:
    """Insert + commit a new conversation row (messages start empty —
    populated asynchronously by the worker container)."""
    conversation = models.AIConversation(
        conversation_id=conversation_id,
        node_kind=node_kind,
        orchestrator_run_id=orchestrator_run_id,
        user_id=user_id,
        tenant_id=tenant_id,
        assigned_worker_id=assigned_worker_id,
        title=title,
        messages=[],
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def append_messages(
    db: Session,
    conversation: models.AIConversation,
    *,
    user_message: str,
    ai_response: str,
    worker_id: Optional[str] = None,
) -> models.AIConversation:
    """Append a user/assistant message pair to a conversation and commit."""
    now = datetime.utcnow().isoformat()
    messages = conversation.messages or []

    messages.append({
        "role": "user",
        "content": user_message,
        "timestamp": now,
    })
    messages.append({
        "role": "assistant",
        "content": ai_response,
        "timestamp": now,
    })

    conversation.messages = messages
    conversation.last_activity = datetime.utcnow()

    if worker_id:
        conversation.assigned_worker_id = worker_id

    db.commit()
    return conversation


def soft_delete_conversation(db: Session, conversation: models.AIConversation) -> None:
    """ISO 27001: no hard deletes (MEMORY iso-no-hard-delete) — the
    user-facing delete just stamps ``deleted_at``."""
    conversation.deleted_at = datetime.utcnow()
    db.commit()


def hard_delete_conversation(db: Session, conversation: models.AIConversation) -> None:
    """Retire a throwaway analyze-dag conversation (dispatch-failure or
    poll-timeout cleanup).

    iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    iso-no-hard-delete) — even these ephemeral, never-shown rows are now
    soft-deleted rather than dropped. `AIConversation` is in database.py's
    soft-delete listener, so a tombstoned conversation disappears from every
    live read exactly as a hard delete would. Name kept for its callers."""
    conversation.deleted_at = datetime.utcnow()
    db.commit()


def get_workflow_run_by_orchestrator_id(
    db: Session, orchestrator_run_id: str,
) -> Optional[models.WorkflowRun]:
    return (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.orchestrator_run_id == orchestrator_run_id)
        .first()
    )


def list_failed_tasks_for_run(db: Session, run_id: int) -> List[models.ApiTaskQueue]:
    return (
        db.query(models.ApiTaskQueue)
        .filter(
            models.ApiTaskQueue.workflow_run_id == run_id,
            models.ApiTaskQueue.status.in_([models.ApiTaskStatus.FAILED]),
        )
        .all()
    )


def create_ai_task(db: Session, task: models.ApiTaskQueue) -> models.ApiTaskQueue:
    """Persist a new ``ApiTaskQueue`` row and commit — the one write this
    domain issues to dispatch a task to the Go worker's CPU queue."""
    db.add(task)
    db.commit()
    return task
