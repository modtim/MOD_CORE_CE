"""CRUD layer for the workflow-wizard "apply" sub-module (Gate 2b
migration of ``routers/support_chat/node_wizard.py`` — see
``services/_base.py`` for the layering contract this follows).

The ONLY place ``db.query(models.X)`` lives for this slice of the
support_chat domain: the wizard session lookup and the session's
latest assistant-turn lookup used by ``POST
/support/workflow-wizard/apply``.

``get_session_by_id`` is intentionally caller-UNSCOPED (no ``user_id``
filter baked into the query) — the service layer
(``services/support_chat_node_wizard_service.py``) applies ownership
policy on top, so a legitimate 403 never collapses into a 404
(migrate_rules #5).

``SupportChatSession`` / ``SupportChatMessage`` have no ``tenant_id``
or ``deleted_at`` column (no tenant scoping or soft-delete on this
domain today, same as ``crud/support_chat_workflows.py``) — filters
are ported 1:1 from the pre-migration inline queries, nothing added.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_session_by_id(db: Session, session_id: str) -> Optional[models.SupportChatSession]:
    """Fetch one support-chat session by id. Unscoped by design."""
    return (
        db.query(models.SupportChatSession)
          .filter(models.SupportChatSession.id == session_id)
          .first()
    )


def get_latest_assistant_message(
    db: Session, session_id: str,
) -> Optional[models.SupportChatMessage]:
    """Most recent assistant-role message in a session (by primary key,
    matching the pre-migration ``order_by(id.desc()).first()``) — the
    turn the apply handler reads its stored proposal from.
    """
    return (
        db.query(models.SupportChatMessage)
          .filter(
              models.SupportChatMessage.session_id == session_id,
              models.SupportChatMessage.role == "assistant",
          )
          .order_by(models.SupportChatMessage.id.desc())
          .first()
    )
