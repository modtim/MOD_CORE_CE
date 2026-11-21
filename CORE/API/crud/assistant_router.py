"""CRUD layer for the assistant admin/config/usage sub-module (Gate 2b
migration of ``routers/assistant.py`` — see services/_base.py for the
layering contract this follows).

The ONLY place ``db.query(models.X)`` lives for this slice of the
assistant domain: per-tenant config lookup, the caller's resumable
session + its transcript, and the admin usage roll-up (session/message
counts + per-model token totals).

Named ``assistant_router`` (not ``assistant``) to avoid clashing with
``crud/ai_assistant.py`` (a different, already-migrated router) and
``crud/assistant_support.py`` (the support-chat sub-module).

Every lookup here already scopes on the CALLER's own ``tenant_id`` (no
fetch-by-id of another tenant's/user's row happens on this surface), so
the 404-vs-403 trap (migrate_rules #5) — which applies to cross-owner
fetch-by-id — does not arise in this domain the way it does for, e.g.,
session-by-id ownership checks.

``AssistantTenantConfig`` / ``AssistantSession`` / ``AssistantMessage``
have no ``deleted_at`` column (no soft-delete on this domain today).
"""
from __future__ import annotations

import datetime as _dt
from typing import List, Optional, Sequence

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

import models


def get_tenant_config(db: Session, tenant_id: int) -> Optional[models.AssistantTenantConfig]:
    """Fetch the tenant's assistant config row.

    Exactly one row per tenant is expected — backfilled by
    ``database._migrate_schema()`` at startup — but callers must still
    handle ``None`` defensively (the row can be missing if a tenant slipped
    through the backfill).
    """
    return (
        db.query(models.AssistantTenantConfig)
        .filter(models.AssistantTenantConfig.tenant_id == tenant_id)
        .one_or_none()
    )


def get_recent_session(
    db: Session, *, user_id: int, tenant_id: int, cutoff: _dt.datetime,
) -> Optional[models.AssistantSession]:
    """Most-recently-active session for (user, tenant) whose ``last_active_at``
    is within the retention cutoff, or ``None`` if there isn't one."""
    return (
        db.query(models.AssistantSession)
        .filter(
            models.AssistantSession.user_id == user_id,
            models.AssistantSession.tenant_id == tenant_id,
            models.AssistantSession.last_active_at >= cutoff,
        )
        .order_by(models.AssistantSession.last_active_at.desc())
        .first()
    )


def list_session_messages(db: Session, session_id) -> List[models.AssistantMessage]:
    """User/assistant turns for a session, oldest first. Tool messages are
    internal and excluded at the query level (never returned to a panel)."""
    return (
        db.query(models.AssistantMessage)
        .filter(
            models.AssistantMessage.session_id == session_id,
            models.AssistantMessage.role.in_(("user", "assistant")),
        )
        .order_by(models.AssistantMessage.created_at.asc())
        .all()
    )


def count_sessions_since(db: Session, *, tenant_id: int, cutoff: _dt.datetime) -> int:
    """Count of the tenant's sessions started on/after ``cutoff``."""
    return (
        db.query(sa_func.count(models.AssistantSession.id))
        .filter(models.AssistantSession.tenant_id == tenant_id)
        .filter(models.AssistantSession.started_at >= cutoff)
        .scalar()
        or 0
    )


def count_messages_since(db: Session, *, tenant_id: int, cutoff: _dt.datetime) -> int:
    """Count of the tenant's messages created on/after ``cutoff`` (any role)."""
    return (
        db.query(models.AssistantMessage)
        .join(
            models.AssistantSession,
            models.AssistantMessage.session_id == models.AssistantSession.id,
        )
        .filter(models.AssistantSession.tenant_id == tenant_id)
        .filter(models.AssistantMessage.created_at >= cutoff)
        .count()
    )


def usage_by_model_since(
    db: Session, *, tenant_id: int, cutoff: _dt.datetime,
) -> Sequence:
    """Per-model token/message roll-up for the tenant's assistant-authored
    messages created on/after ``cutoff``.

    Returns rows with ``.model_id``, ``.prompt``, ``.completion``,
    ``.messages`` attributes (one row per distinct ``model_id``, including
    ``None`` for messages whose provider never stamped one).
    """
    return (
        db.query(
            models.AssistantMessage.model_id,
            sa_func.coalesce(sa_func.sum(models.AssistantMessage.prompt_tokens), 0).label("prompt"),
            sa_func.coalesce(sa_func.sum(models.AssistantMessage.completion_tokens), 0).label("completion"),
            sa_func.count(models.AssistantMessage.id).label("messages"),
        )
        .join(
            models.AssistantSession,
            models.AssistantMessage.session_id == models.AssistantSession.id,
        )
        .filter(models.AssistantSession.tenant_id == tenant_id)
        .filter(models.AssistantMessage.created_at >= cutoff)
        .filter(models.AssistantMessage.role == "assistant")
        .group_by(models.AssistantMessage.model_id)
        .all()
    )
