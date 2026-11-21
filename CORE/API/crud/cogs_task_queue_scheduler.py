"""CRUD operations for `routers/cogs/task_queue_scheduler.py` (openspec:
stack-training-and-prewarm, Gate 2b).

The ONLY place `db.query(...)` lives for this sub-module's one endpoint:

    - GET /cogs/{cog_id}/pdf-view  (public_router — iframe PDF preview)

Tenant scoping matches the pre-migration inline queries EXACTLY: the File
and ContentBlob lookups are tenant-scoped (the pre-migration code filtered
on `current_user.tenant_id`), while the Version lookup is intentionally
tenant-UNSCOPED because it is only reached via a cog already resolved
through `crud.get_cog_by_id_and_tenant` (tenant-checked) — do not add a
filter there without checking that call site.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

import models


def get_active_file_for_cog(
    db: Session, *, cog_id: int, tenant_id: int
) -> Optional["models.File"]:
    """The active (non-soft-deleted) File row for a cog, tenant-scoped."""
    return (
        db.query(models.File)
        .filter(
            models.File.cog_id == cog_id,
            models.File.tenant_id == tenant_id,
            models.File.deleted_at.is_(None),
        )
        .first()
    )


def get_version_by_id(db: Session, version_id: int) -> Optional["models.Version"]:
    """A Version by id, tenant-UNSCOPED (pre-migration behavior — reached
    only via an already tenant-checked cog's `latest_version_id`)."""
    return db.query(models.Version).filter(models.Version.id == version_id).first()


def get_content_blob(
    db: Session, *, content_id: Any, tenant_id: int
) -> Optional["models.ContentBlob"]:
    """A ContentBlob by id (content hash), tenant-scoped."""
    return (
        db.query(models.ContentBlob)
        .filter(
            models.ContentBlob.id == content_id,
            models.ContentBlob.tenant_id == tenant_id,
        )
        .first()
    )
