"""CRUD layer for the assets/cogs/files domain (openspec: stack-training-and-prewarm,
Gate 2 / D9).

The ONLY place `db.query(...)` lives for the inline query sites the audit
found still living in `routers/cogs/assets_cogs_files.py`'s handler bodies.
Each function here is pure data access — tenant/soft-delete scoping in, rows
out — no policy, no HTTP. Business rules stay in
`services/assets_cogs_files_service.py`.

This is a SIBLING of the pre-existing `crud/cog_cud.py`, `crud/file_cud.py`,
`crud/tag_cud.py`, `crud/shareable_link_cud.py` — it does not duplicate what
those already cover (the router already calls those via the `crud` package
for several endpoints); it covers the remaining inline `db.query(...)`
anti-pattern sites the deep audit flagged (docs/api-audit/deep/
domain-clusters.md: assets_cogs_files, 109 endpoints in the broader domain,
~52 / ~73 inline queries in this file).

Only a subset of this file's endpoints were migrated this pass — see the
migration report for what was deferred and why (streaming/upload/background-
thread endpoints are watched separately per the migration rules).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func as sa_func, or_
from sqlalchemy.orm import Session

import models


# =============================================================================
# list_cogs
# =============================================================================


def list_deleted_cogs(
    db: Session,
    *,
    tenant_id: int,
    owner_id: Optional[int],
    search: Optional[str],
    sort_by: str,
    order: str,
    limit: int,
    offset: int,
) -> List[models.Cog]:
    """Soft-deleted cogs for the tenant's "Deleted" view.

    `owner_id=None` means every soft-deleted cog in the tenant (the
    tenant-manager / platform-admin view); otherwise scoped to that owner.
    """
    query = db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.isnot(None),
    )
    if owner_id is not None:
        query = query.filter(models.Cog.owner_id == owner_id)
    if search:
        query = query.filter(models.Cog.name.ilike(f"%{search}%"))
    if hasattr(models.Cog, sort_by):
        column = getattr(models.Cog, sort_by)
        query = query.order_by(column.desc() if order == "desc" else column.asc())
    return query.offset(offset).limit(limit).all()


def list_owned_cogs(
    db: Session,
    *,
    owner_id: int,
    search: Optional[str],
    selectable_inputs_only: bool,
    sort_by: str,
    order: str,
    limit: int,
    offset: int,
) -> List[models.Cog]:
    """`all_items=True` path: every non-deleted cog the user owns, ignoring
    `parent_id`."""
    query = db.query(models.Cog).filter(
        models.Cog.owner_id == owner_id,
        models.Cog.deleted_at.is_(None),
        # GX.R4 — hidden cogs (ephemeral run inputs, wizard demo samples) never
        # appear in the user's library, on any listing surface.
        models.Cog.is_hidden.isnot(True),
    )
    if search:
        query = query.filter(models.Cog.name.ilike(f"%{search}%"))
    if selectable_inputs_only:
        query = query.filter(or_(
            models.Cog.cog_type != models.CogType.FILE,
            models.Cog.latest_version_id.isnot(None),
        ))
    if hasattr(models.Cog, sort_by):
        column = getattr(models.Cog, sort_by)
        query = query.order_by(column.desc() if order == "desc" else column.asc())
    return query.offset(offset).limit(limit).all()


def list_visible_children(
    db: Session,
    *,
    parent_id: Optional[int],
    tenant_id: int,
    search: Optional[str],
    selectable_inputs_only: bool,
    owner_id: int,
    grant_cog_ids: Optional[Sequence[int]],
    sort_by: str,
    order: str,
    limit: int,
    offset: int,
) -> List[models.Cog]:
    """Default listing: children of `parent_id` the caller can see.

    `grant_cog_ids=None` means the parent itself is viewable (owner or a
    folder grant), so ALL of its children are returned (folder grants
    cascade). Otherwise the result is narrowed to cogs the caller owns or
    holds a direct grant on (`grant_cog_ids`).
    """
    base = db.query(models.Cog).filter(
        models.Cog.parent_id == parent_id,
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
        # GX.R4 — hidden cogs (ephemeral run inputs) are excluded from the
        # default children listing (they live at the tenant root, parent_id
        # NULL, so the parent-filtered view never sees them anyway; this keeps
        # the all_items / search / root views consistent too).
        models.Cog.is_hidden.isnot(True),
    )
    if search:
        base = base.filter(models.Cog.name.ilike(f"%{search}%"))
    if selectable_inputs_only:
        base = base.filter(or_(
            models.Cog.cog_type != models.CogType.FILE,
            models.Cog.latest_version_id.isnot(None),
        ))
    if grant_cog_ids is not None:
        base = base.filter(or_(
            models.Cog.owner_id == owner_id,
            models.Cog.id.in_(grant_cog_ids),
        ))
    if hasattr(models.Cog, sort_by):
        column = getattr(models.Cog, sort_by)
        base = base.order_by(column.desc() if order == "desc" else column.asc())
    return base.offset(offset).limit(limit).all()


def file_sizes_by_cog(db: Session, cog_ids: Sequence[int]) -> Dict[int, int]:
    """Max `size_bytes` per FILE-type cog id (one active File row expected)."""
    return dict(
        db.query(models.File.cog_id, sa_func.max(models.File.size_bytes))
        .filter(
            models.File.cog_id.in_(cog_ids),
            models.File.deleted_at.is_(None),
        )
        .group_by(models.File.cog_id)
        .all()
    )


def folder_totals_by_parent(db: Session, folder_cog_ids: Sequence[int]) -> Dict[int, int]:
    """Sum of direct file children's `size_bytes`, keyed by parent (folder) cog id."""
    return dict(
        db.query(
            models.Cog.parent_id,
            sa_func.coalesce(sa_func.sum(models.File.size_bytes), 0),
        )
        .join(models.File, models.File.cog_id == models.Cog.id)
        .filter(
            models.Cog.parent_id.in_(folder_cog_ids),
            models.File.deleted_at.is_(None),
            models.Cog.deleted_at.is_(None),
        )
        .group_by(models.Cog.parent_id)
        .all()
    )


def blob_format_meta_by_cog(
    db: Session, file_cog_ids: Sequence[int]
) -> Dict[int, Tuple[Optional[str], Optional[str]]]:
    """`(file_extension, mime_type)` from ContentBlob, keyed by cog id."""
    rows = (
        db.query(
            models.ContentBlob.cog_id,
            sa_func.max(models.ContentBlob.file_extension),
            sa_func.max(models.ContentBlob.mime_type),
        )
        .filter(models.ContentBlob.cog_id.in_(file_cog_ids))
        .group_by(models.ContentBlob.cog_id)
        .all()
    )
    return {row[0]: (row[1], row[2]) for row in rows}


def file_format_meta_by_cog(
    db: Session, file_cog_ids: Sequence[int]
) -> Dict[int, Tuple[Optional[str], Optional[str]]]:
    """`(mime_type, name)` fallback from File, keyed by cog id."""
    rows = (
        db.query(
            models.File.cog_id,
            sa_func.max(models.File.mime_type),
            sa_func.max(models.File.name),
        )
        .filter(
            models.File.cog_id.in_(file_cog_ids),
            models.File.deleted_at.is_(None),
        )
        .group_by(models.File.cog_id)
        .all()
    )
    return {row[0]: (row[1], row[2]) for row in rows}


# =============================================================================
# search_cogs_recursive
# =============================================================================


def list_tenant_cogs_for_search(db: Session, *, tenant_id: int) -> List[models.Cog]:
    """Every non-deleted cog in the tenant — the recursive search's candidate
    set (permission + name/tag matching stays in the service, in-memory, to
    match the pre-migration behaviour exactly)."""
    return db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
        # GX.R4 — hidden cogs (ephemeral run inputs) are not searchable in the
        # library; they are run-scoped, not user assets.
        models.Cog.is_hidden.isnot(True),
    ).all()


# =============================================================================
# get_cog — authoritative format metadata
# =============================================================================


def blob_format_for_cog(db: Session, cog_id: int) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """`(file_extension, mime_type)` from ContentBlob for a single cog."""
    return (
        db.query(models.ContentBlob.file_extension, models.ContentBlob.mime_type)
        .filter(models.ContentBlob.cog_id == cog_id)
        .first()
    )


def file_format_fallback_for_cog(db: Session, cog_id: int) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """`(mime_type, name)` fallback from File for a single cog."""
    return (
        db.query(models.File.mime_type, models.File.name)
        .filter(
            models.File.cog_id == cog_id,
            models.File.deleted_at.is_(None),
        )
        .first()
    )


# =============================================================================
# tags (add/remove — the Tag lookup only; list/create already go via the
# pre-existing crud package)
# =============================================================================


def get_tag_by_id_and_tenant(db: Session, *, tag_id: int, tenant_id: int) -> Optional[models.Tag]:
    """Fetch a tag by id, scoped to the tenant.

    Returns None both when the tag doesn't exist AND when it belongs to a
    different tenant — the caller maps both to 404, matching the
    pre-migration behaviour exactly (no tenant existence leak).
    """
    tag = db.query(models.Tag).filter(models.Tag.id == tag_id).first()
    if tag is None or tag.tenant_id != tenant_id:
        return None
    return tag


# =============================================================================
# search-by-processing / processing-analytics
# =============================================================================


def search_cogs_by_processing(
    db: Session,
    *,
    tenant_id: int,
    workflow_name: Optional[str],
    node_kind: Optional[str],
    status: Optional[str],
    min_cost: Optional[int],
    max_cost: Optional[int],
    processed_after,
    has_errors: Optional[bool],
    limit: int,
    offset: int,
) -> List[models.Cog]:
    """Asset search over `processing_metadata` via PostgreSQL JSONB `@>`."""
    query = db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
    )
    if workflow_name:
        query = query.filter(
            models.Cog.processing_metadata['processing_history'].op('@>')(
                f'[{{"workflow_name": "{workflow_name}"}}]'
            )
        )
    if node_kind:
        query = query.filter(
            models.Cog.processing_metadata['processing_history'].op('@>')(
                f'[{{"nodes_executed": [{{"node_kind": "{node_kind}"}}]}}]'
            )
        )
    if status:
        query = query.filter(
            models.Cog.processing_metadata['processing_history'].op('@>')(
                f'[{{"status": "{status}"}}]'
            )
        )
    if min_cost is not None:
        query = query.filter(models.Cog.total_processing_cost >= min_cost)
    if max_cost is not None:
        query = query.filter(models.Cog.total_processing_cost <= max_cost)
    if processed_after:
        query = query.filter(models.Cog.last_processed_at >= processed_after)
    if has_errors is not None:
        if has_errors:
            query = query.filter(
                models.Cog.processing_metadata['processing_history'].op('@>')(
                    '[{"status": "failure"}]'
                )
            )
        else:
            query = query.filter(
                ~models.Cog.processing_metadata['processing_history'].op('@>')(
                    '[{"status": "failure"}]'
                )
            )
    return query.offset(offset).limit(limit).all()


def list_cogs_for_processing_analytics(
    db: Session,
    *,
    tenant_id: int,
    start_date,
    end_date,
) -> List[models.Cog]:
    query = db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
    )
    if start_date:
        query = query.filter(models.Cog.last_processed_at >= start_date)
    if end_date:
        query = query.filter(models.Cog.last_processed_at <= end_date)
    return query.all()


# =============================================================================
# folder/files recursive (zip-download service endpoint)
# =============================================================================


def get_active_folder(db: Session, folder_id: int) -> Optional[models.Cog]:
    return db.query(models.Cog).filter(
        models.Cog.id == folder_id,
        models.Cog.deleted_at.is_(None),
    ).first()


def get_active_children(db: Session, parent_cog_id: int) -> List[models.Cog]:
    return db.query(models.Cog).filter(
        models.Cog.parent_id == parent_cog_id,
        models.Cog.deleted_at.is_(None),
    ).all()


def get_version_by_id(db: Session, version_id: int) -> Optional[models.Version]:
    return db.query(models.Version).filter(models.Version.id == version_id).first()


def get_content_blob_for_version(db: Session, *, tenant_id: int, content_id) -> Optional[models.ContentBlob]:
    return db.query(models.ContentBlob).filter(
        models.ContentBlob.tenant_id == tenant_id,
        models.ContentBlob.id == content_id,
    ).first()


# =============================================================================
# legal hold
# =============================================================================


def get_active_file_records_for_cog(db: Session, *, cog_id: int, tenant_id: int) -> List[models.File]:
    return db.query(models.File).filter(
        models.File.cog_id == cog_id,
        models.File.tenant_id == tenant_id,
        models.File.deleted_at.is_(None),
    ).all()


def get_workflow_outputs_by_hashes(
    db: Session,
    *,
    content_hashes: Sequence[str],
    tenant_id: int,
    legal_hold: Optional[bool] = None,
) -> List["models.WorkflowNodeOutput"]:
    query = db.query(models.WorkflowNodeOutput).filter(
        models.WorkflowNodeOutput.content_hash.in_(content_hashes),
        models.WorkflowNodeOutput.tenant_id == tenant_id,
    )
    if legal_hold is not None:
        query = query.filter(models.WorkflowNodeOutput.legal_hold == legal_hold)
    return query.all()


def get_first_held_workflow_output(
    db: Session, *, content_hashes: Sequence[str], tenant_id: int
) -> Optional["models.WorkflowNodeOutput"]:
    return db.query(models.WorkflowNodeOutput).filter(
        models.WorkflowNodeOutput.content_hash.in_(content_hashes),
        models.WorkflowNodeOutput.tenant_id == tenant_id,
        models.WorkflowNodeOutput.legal_hold == True,  # noqa: E712 (SQLAlchemy comparison)
    ).first()


def count_held_workflow_outputs(
    db: Session, *, content_hashes: Sequence[str], tenant_id: int
) -> int:
    return db.query(models.WorkflowNodeOutput).filter(
        models.WorkflowNodeOutput.content_hash.in_(content_hashes),
        models.WorkflowNodeOutput.tenant_id == tenant_id,
        models.WorkflowNodeOutput.legal_hold == True,  # noqa: E712
    ).count()


# =============================================================================
# thumbnail upload
# =============================================================================


def get_content_blob_for_cog(db: Session, *, cog_id: int, tenant_id: int) -> Optional[models.ContentBlob]:
    return db.query(models.ContentBlob).filter(
        models.ContentBlob.cog_id == cog_id,
        models.ContentBlob.tenant_id == tenant_id,
    ).first()
