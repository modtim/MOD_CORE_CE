"""CRUD operations for `routers/cogs/workflows.py` (openspec:
stack-training-and-prewarm, Gate 2b).

The ONLY place `db.query(...)` lives for this sub-module's three endpoints:

    - POST /cogs/files              (get_multiple_cogs_files, service-to-service
                                      zip-download helper)
    - GET  /cogs/{cog_id}/preview-url
    - GET  /cogs/{cog_id}/text-content

Tenant scoping here matches the pre-migration inline queries EXACTLY —
several of these lookups are intentionally tenant-UNSCOPED because the
router/service already enforces tenant safety another way (the zip-download
walk calls `pin_run_task_tenant` per-cog; `get_active_cog` there simply
excludes soft-deleted rows). Do not add a tenant filter here without also
checking the caller — that would change behavior, which this migration must
not do.
"""
from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy.orm import Session

import models


def get_cogs_by_ids(db: Session, cog_ids: List[int]) -> List["models.Cog"]:
    """All cogs matching the given ids, tenant-UNSCOPED — the caller
    (get_multiple_cogs_files) checks tenant per-row via pin_run_task_tenant."""
    return db.query(models.Cog).filter(models.Cog.id.in_(cog_ids)).all()


def get_active_cog(db: Session, cog_id: int) -> Optional["models.Cog"]:
    """A single non-soft-deleted cog by id, tenant-UNSCOPED."""
    return (
        db.query(models.Cog)
        .filter(models.Cog.id == cog_id, models.Cog.deleted_at.is_(None))
        .first()
    )


def get_active_children(db: Session, parent_id: int) -> List["models.Cog"]:
    """Active (non-soft-deleted) direct children of a cog, tenant-UNSCOPED."""
    return (
        db.query(models.Cog)
        .filter(models.Cog.parent_id == parent_id, models.Cog.deleted_at.is_(None))
        .all()
    )


def get_version_by_id(db: Session, version_id: int) -> Optional["models.Version"]:
    return db.query(models.Version).filter(models.Version.id == version_id).first()


def get_content_blob_for_version(
    db: Session, *, tenant_id: int, content_id: Any
) -> Optional["models.ContentBlob"]:
    return (
        db.query(models.ContentBlob)
        .filter(models.ContentBlob.tenant_id == tenant_id, models.ContentBlob.id == content_id)
        .first()
    )


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
