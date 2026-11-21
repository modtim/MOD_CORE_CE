"""Service layer for the workflow-wizard "apply" sub-module (Gate 2b
migration of ``routers/support_chat/node_wizard.py``).

Owns the session-ownership + kind policy (404-vs-403-vs-409) the
handler previously inlined next to its ``db.query`` calls. See
``services/_base.py`` for the layering contract and the 404-vs-403
trap this follows: the CRUD fetch is UNSCOPED and ownership is
enforced here so a legitimate 403 never collapses into a 404
(migrate_rules #5).

Everything else the handler does — resolving/enriching proposal nodes,
credit charging, feedback/metric recording, the merge-vs-replace
branch — is orchestration already routed through the shared helpers in
``routers/support_chat/_shared.py`` (already migrated per this gate)
and stays in the router, matching the precedent set by
``services/support_chat_workflows_service.py``.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

import models
from crud import support_chat_node_wizard as crud
from services._base import ConflictError, NotFoundError, ServiceError


class ForbiddenError(ServiceError):
    """Caller is authenticated but not entitled to the resource (router
    maps -> 403). ``services/_base.py`` ships ``NotFoundError`` /
    ``ConflictError`` but no forbidden error — declared here per
    migrate_rules #5, same pattern as
    ``services/support_chat_workflows_service.py``."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


def get_owned_wizard_session(
    db: Session, session_id: str, user_id: int,
) -> models.SupportChatSession:
    """Fetch a wizard session the caller owns.

    Raises ``NotFoundError`` if the session doesn't exist,
    ``ForbiddenError`` if it belongs to someone else, and
    ``ConflictError`` if it isn't a wizard-kind session — the same
    three checks the handler did inline before the migration, in the
    same order.
    """
    sess = crud.get_session_by_id(db, session_id)
    if sess is None:
        raise NotFoundError("Session not found")
    if sess.user_id != user_id:
        raise ForbiddenError("Not your session")
    if sess.kind != "wizard":
        raise ConflictError("Session is not a wizard session")
    return sess


def get_latest_assistant_message(
    db: Session, session_id: str,
) -> Optional[models.SupportChatMessage]:
    """Most recent assistant turn in a session — the source of the
    stored proposal the apply handler materializes."""
    return crud.get_latest_assistant_message(db, session_id)
