"""CRUD operations for `routers/cogs/_shared.py` (openspec:
stack-training-and-prewarm, Gate 2b).

The ONLY place `db.query(...)` lives for the shared cogs helpers used across
every cogs sub-module: `_get_or_create_tenant_root_folder`,
`_attach_original_paths`, `_find_name_collision`, `_copy_file`,
`_copy_folder_recursive`, `_dispatch_pointcloud_preview_task`,
`_dispatch_sldprt_conversion_task`, `_lazy_dispatch_thumbnail`,
`_ensure_raster_thumbnail`, `_maybe_audit_xtenant_block`.

Tenant scoping matches the pre-migration inline queries EXACTLY, including
several lookups that are intentionally tenant-UNSCOPED (`get_cog_by_id`,
`get_version_by_id`, `get_any_active_file_for_cog`) because the pre-migration
code reached them via an id already resolved through a tenant-checked path
(a cog's own `latest_version_id`, an internal task-dispatch cog_id, or the
platform-admin cross-tenant audit probe that must NOT be tenant-filtered to
do its job). Do not add a tenant filter here without checking every call
site — this migration must not change behavior.
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

import models


def get_tenant_master_root(db: Session, tenant_id: int) -> Optional["models.Cog"]:
    """The tenant's root MASTER_FOLDER (lowest id among non-deleted
    candidates), or None if one hasn't been created yet."""
    return (
        db.query(models.Cog)
        .filter(
            models.Cog.tenant_id == tenant_id,
            models.Cog.parent_id.is_(None),
            models.Cog.cog_type == models.CogType.MASTER_FOLDER,
            models.Cog.deleted_at.is_(None),
        )
        .order_by(models.Cog.id.asc())
        .first()
    )


def get_cog_id_name_parent_pairs(
    db: Session, *, tenant_id: int, ids: Sequence[int]
) -> List[Tuple[int, str, Optional[int]]]:
    """(id, name, parent_id) rows for the given ids, tenant-scoped. Ancestor
    lookups intentionally do NOT filter on `deleted_at` — a soft-deleted
    ancestor still needs to contribute its name to a displayed path."""
    return (
        db.query(models.Cog.id, models.Cog.name, models.Cog.parent_id)
        .filter(
            models.Cog.tenant_id == tenant_id,
            models.Cog.id.in_(list(ids)),
        )
        .all()
    )


def find_name_collision(
    db: Session,
    *,
    name: str,
    parent_id: Optional[int],
    tenant_id: int,
    exclude_cog_id: Optional[int] = None,
) -> Optional["models.Cog"]:
    """A non-deleted sibling with the same name in the destination folder."""
    q = db.query(models.Cog).filter(
        models.Cog.name == name,
        models.Cog.parent_id == parent_id,
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
    )
    if exclude_cog_id is not None:
        q = q.filter(models.Cog.id != exclude_cog_id)
    return q.first()


def get_cog_by_id(db: Session, cog_id: int) -> Optional["models.Cog"]:
    """A cog by id, tenant-UNSCOPED. Pre-migration callers already trust
    `cog_id` (cross-tenant audit probe, internal task-dispatch metadata
    updates) — adding a tenant filter here would change behavior."""
    return db.query(models.Cog).filter(models.Cog.id == cog_id).first()


def get_any_active_file_for_cog(db: Session, cog_id: int) -> Optional["models.File"]:
    """Any non-soft-deleted File row for a cog, tenant-UNSCOPED (pre-migration
    `_copy_file` orphan-content guard)."""
    return (
        db.query(models.File)
        .filter(models.File.cog_id == cog_id, models.File.deleted_at.is_(None))
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


def get_version_by_id(db: Session, version_id: int) -> Optional["models.Version"]:
    """A Version by id, tenant-UNSCOPED (pre-migration behavior — reached only
    via an already tenant-checked cog's `latest_version_id`)."""
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


def get_active_children_by_owner(
    db: Session, *, parent_id: int, owner_id: int
) -> List["models.Cog"]:
    """Active (non-soft-deleted) direct children of a cog, owned by
    `owner_id` (pre-migration `_copy_folder_recursive` behavior — a copy only
    propagates the acting owner's own children)."""
    return (
        db.query(models.Cog)
        .filter(
            models.Cog.parent_id == parent_id,
            models.Cog.owner_id == owner_id,
            models.Cog.deleted_at.is_(None),
        )
        .all()
    )
