"""Service layer for the workflow-wizard manual test-run sub-module
(Gate 2b migration of ``routers/support_chat/workflows.py``).

Owns the session-ownership policy (404-vs-403) and the proposal-graph /
standin-ContentBlob decision logic the handler previously inlined next
to its ``db.query`` calls. See ``services/_base.py`` for the layering
contract and the 404-vs-403 trap this follows: the CRUD fetch is
UNSCOPED and ownership is enforced here so a legitimate 403 never
collapses into a 404 (migrate_rules #5).

Everything else the handler does — rate limiting, wizard test-input
selection, node-definition resolution/enrichment, Workflow /
WorkflowVersion / WorkflowRun creation (already routed through the
top-level ``crud`` module, not a raw query), and the background
dispatch thread — is orchestration that stays in the router, matching
the precedent set by ``routers/support_chat/assistant_support.py``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

import models
from crud import support_chat_workflows as crud
from services._base import NotFoundError, ServiceError


class ForbiddenError(ServiceError):
    """Caller is authenticated but not entitled to the resource (router
    maps -> 403). ``services/_base.py`` ships ``NotFoundError`` /
    ``ConflictError`` but no forbidden error — declared here per
    migrate_rules #5."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


def get_owned_session(
    db: Session, session_id: str, user_id: int,
) -> models.SupportChatSession:
    """Fetch a session the caller owns.

    Raises ``NotFoundError`` if the session doesn't exist and
    ``ForbiddenError`` if it belongs to someone else — the same check
    the handler did inline before the migration.
    """
    sess = crud.get_session_by_id(db, session_id)
    if sess is None:
        raise NotFoundError("session not found")
    if sess.user_id != user_id:
        raise ForbiddenError("not your session")
    return sess


def find_latest_proposal_graph(
    db: Session, session_id: str,
) -> Optional[Dict[str, Any]]:
    """Scan the session's assistant turns (most recent first) for the
    last stored wizard proposal graph.

    Returns ``None`` when no turn carried one — the router maps that to
    a 422 ("nothing to test yet" is a request-shape problem, not an
    ownership/existence error, so it does not go through a ServiceError).
    """
    assistant_msgs = crud.list_assistant_messages(db, session_id)
    for msg in assistant_msgs:
        tu = msg.token_usage or {}
        p = tu.get("proposal")
        if p and isinstance(p, dict) and p.get("nodes"):
            return {
                "nodes": p.get("nodes", []),
                "edges": p.get("edges", []),
            }
    return None


def get_or_create_standin_cog(
    db: Session, *, tenant_id: int, session_id: str, descriptor: Any,
) -> int:
    """Resolve the source Cog id for a generated (non-library) wizard
    test input: reuse a ContentBlob already stored under this
    content-hash, or mint a hidden, system-generated standin Cog +
    Version wrapping one.

    Preserves the pre-migration handler's write sequence exactly,
    including the pre-existing redundant second ``db.add(blob);
    db.flush()`` immediately after the first — harmless (SQLAlchemy
    no-ops re-adding an already-pending/persistent instance) but not
    "fixed" here per migrate_rules (don't change behavior while
    migrating).
    """
    ext = getattr(descriptor, "file_extension", "bin")
    mime = getattr(descriptor, "mime_type", "application/octet-stream")
    content_hash = getattr(descriptor, "content_hash", None) or descriptor.minio_pointer

    blob = crud.get_content_blob_by_hash(db, content_hash, tenant_id)
    if blob is None:
        blob = models.ContentBlob(
            id=content_hash,
            tenant_id=tenant_id,
            file_extension=ext, mime_type=mime,
            storage_path=descriptor.minio_pointer,
            size_bytes=getattr(descriptor, "generated_size_bytes", None) or 0,
        )
        db.add(blob)
        db.flush()
    db.add(blob)
    db.flush()
    cog = models.Cog(
        tenant_id=tenant_id,
        name=f"_wizard_standin_manual_{session_id[:8]}",
        cog_type=models.CogType.FILE,
        is_system_generated=True, is_hidden=True,
    )
    db.add(cog)
    db.flush()
    ver = models.Version(
        cog_id=cog.id, tenant_id=tenant_id,
        content_id=blob.id, version_number=1,
    )
    db.add(ver)
    db.flush()
    cog.latest_version_id = ver.id
    db.flush()
    return cog.id
