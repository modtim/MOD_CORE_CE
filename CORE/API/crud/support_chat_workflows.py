"""CRUD layer for the workflow-wizard manual test-run sub-module (Gate 2b
migration of ``routers/support_chat/workflows.py`` — see
``services/_base.py`` for the layering contract this follows).

The ONLY place ``db.query(models.X)`` lives for this slice of the
support_chat domain: session lookup for the manual wizard test-run
endpoint, the session's assistant-turn history (to recover the last
stored proposal graph), and the tenant-scoped ContentBlob dedup lookup
used when minting a standin test-input Cog.

``get_session_by_id`` is intentionally caller-UNSCOPED (no ``user_id``
filter baked into the query) — the service layer
(``services/support_chat_workflows_service.py``) applies ownership
policy on top, so a legitimate 403 never collapses into a 404
(migrate_rules #5).

``SupportChatSession`` / ``SupportChatMessage`` / ``ContentBlob`` have no
``deleted_at`` column (no soft-delete on this domain today) — filters
are ported 1:1 from the pre-migration inline queries, nothing added.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_session_by_id(db: Session, session_id: str) -> Optional[models.SupportChatSession]:
    """Fetch one support-chat session by id. Unscoped by design."""
    return (
        db.query(models.SupportChatSession)
          .filter(models.SupportChatSession.id == session_id)
          .first()
    )


def list_assistant_messages(
    db: Session, session_id: str,
) -> List[models.SupportChatMessage]:
    """All assistant-role messages for a session, most recent first —
    the order the handler scans in to find the latest stored proposal.
    """
    return (
        db.query(models.SupportChatMessage)
          .filter(
              models.SupportChatMessage.session_id == session_id,
              models.SupportChatMessage.role == "assistant",
          )
          .order_by(models.SupportChatMessage.created_at.desc())
          .all()
    )


def get_content_blob_by_hash(
    db: Session, content_hash: str, tenant_id,
) -> Optional[models.ContentBlob]:
    """Fetch a tenant-owned ContentBlob by its content-hash id — the
    dedup check before minting a standin blob for a wizard test run."""
    return (
        db.query(models.ContentBlob)
          .filter(
              models.ContentBlob.id == content_hash,
              models.ContentBlob.tenant_id == tenant_id,
          )
          .first()
    )
