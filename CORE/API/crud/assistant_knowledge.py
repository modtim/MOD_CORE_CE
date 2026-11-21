"""CRUD layer for the tenant Assistant Knowledge Base (Gate 2b migration of
``routers/assistant_knowledge.py`` — see ``services/_base.py`` for the
layering contract this follows).

The ONLY place ``db.query(models.X)`` lives for this domain: content-addressed
blob dedup lookup (upload/text ingest), folder (Cog) ownership lookup, and
knowledge-document list/get-by-id.

404-vs-403 note (migrate_rules #5): this domain has no doc-level 403 — the
pre-migration behavior already folds "not found" and "wrong tenant" into the
same 404 for both the folder (Cog) lookup and the knowledge-document lookup
(see ``tests/test_assistant_knowledge_isolation.py::test_kb_cross_tenant_delete_is_404``).
There is no legitimate 403 case to protect here (the only 403 in this domain
is the router-level tenant-admin gate, ``require_tenant_admin``), so scoping
these fetches by ``tenant_id`` directly — rather than fetching unscoped and
policing in the service — preserves behavior exactly.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_content_blob(
    db: Session, content_hash: str, tenant_id: int,
) -> Optional[models.ContentBlob]:
    """Fetch an existing content-addressed blob row, tenant-scoped."""
    return (
        db.query(models.ContentBlob)
        .filter(
            models.ContentBlob.id == content_hash,
            models.ContentBlob.tenant_id == tenant_id,
        )
        .first()
    )


def get_owned_cog(db: Session, cog_id: int, tenant_id: int) -> Optional[models.Cog]:
    """Fetch a storage folder (Cog), tenant-scoped + excluding soft-deleted rows."""
    return (
        db.query(models.Cog)
        .filter(
            models.Cog.id == cog_id,
            models.Cog.tenant_id == tenant_id,
            models.Cog.deleted_at.is_(None),
        )
        .first()
    )


def list_tenant_documents(
    db: Session, tenant_id: int,
) -> List[models.TenantKnowledgeDocument]:
    """All of a tenant's knowledge documents, newest first. Unbounded — matches
    pre-migration behavior (this admin surface has no pagination today)."""
    return (
        db.query(models.TenantKnowledgeDocument)
        .filter(models.TenantKnowledgeDocument.tenant_id == tenant_id)
        .order_by(models.TenantKnowledgeDocument.created_at.desc())
        .all()
    )


def get_owned_document(
    db: Session, doc_id: int, tenant_id: int,
) -> Optional[models.TenantKnowledgeDocument]:
    """Fetch one knowledge document, tenant-scoped (see module docstring for
    why this collapses cross-tenant into ``None`` -> 404, not 403)."""
    return (
        db.query(models.TenantKnowledgeDocument)
        .filter(
            models.TenantKnowledgeDocument.id == doc_id,
            models.TenantKnowledgeDocument.tenant_id == tenant_id,
        )
        .one_or_none()
    )
