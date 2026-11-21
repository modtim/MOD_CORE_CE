"""Query layer for the Files upload domain (Gate 2b, routers/files.py).

The ONLY place `db.query(models.Cog)` / `db.query(models.ContentBlob)` lives
for the `/upload` endpoint. Business policy (folder-permission 404-vs-403,
storage-limit enforcement, dedupe-vs-create decisions) lives in
`services/files_service.py`; upload/streaming mechanics stay in
`routers/files.py` per the Gate 2b partial-migration allowance for
upload/download handlers.
"""
from typing import Optional

from sqlalchemy.orm import Session

import models


def get_active_folder(db: Session, cog_id: int, tenant_id: int) -> Optional[models.Cog]:
    """Fetch a non-deleted Cog (folder) by id, scoped to `tenant_id`.

    Returns None if absent, soft-deleted, or owned by another tenant — the
    service maps None to a 404 (never leaking cross-tenant existence).
    """
    return db.query(models.Cog).filter(
        models.Cog.id == cog_id,
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None)
    ).first()


def get_content_blob(db: Session, content_hash: str, tenant_id: int) -> Optional[models.ContentBlob]:
    """Fetch a ContentBlob by its content hash, scoped to `tenant_id`.

    ContentBlob has no `deleted_at` (it predates SoftDeleteMixin and is not in
    database.py's soft-delete listener) -- filters ported 1:1 from the
    original inline query, unchanged.
    """
    return db.query(models.ContentBlob).filter(
        models.ContentBlob.id == content_hash,
        models.ContentBlob.tenant_id == tenant_id
    ).first()
