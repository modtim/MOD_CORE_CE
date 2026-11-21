"""CRUD layer for the REMAINING inline `db.query(...)` sites in
`routers/cogs/assets_cogs_files.py` (openspec: stack-training-and-prewarm,
Gate 2b — largest remaining router sub-module).

This is a SIBLING of `crud/assets_cogs_files.py` (which already covers the
`list_cogs` / `search_cogs_recursive` / `get_cog` format-metadata / tags /
search-by-processing / processing-analytics / folder-files-recursive /
legal-hold sites migrated in an earlier pass) — it does not duplicate that
module. It is named distinctly (`cogs_assets_cogs_files`, not
`assets_cogs_files`) so this pass's additions can't collide with whatever
else is touching that sibling file concurrently.

Covers the remaining ~42 (of 47) inline `db.query(...)` sites this router
file still had: `update_cog`'s ancestor-cycle walk, `delete_cog`,
`restore_cog`, the upload family (multipart-complete, upload-file,
upload-processed-file, upload-text-file, register-uploaded-file,
from-hash, check-by-hash), the conversion/thumbnail service callbacks,
`download_file` / `stream_file`, and the download-job endpoints.

Deferred (NOT covered here — see the migration report): the background-
thread `local_db.query(...)` sites inside `confirm_upload` and
`create_download_job`'s in-process zip worker. Those run on their own
`SessionLocal()` inside a daemon thread closure; extracting them into this
module would not reduce risk (the query is trivial) and moving thread-local
session logic around IS the risk, so they were left untouched per the
migration rules' "leave it if it doesn't separate cleanly" allowance.

Pure data access — tenant/soft-delete scoping in, rows out, no HTTP, no
policy. Business rules live in `services/cogs_assets_cogs_files_service.py`.
Every filter here is a 1:1 port of the pre-migration inline query — where
the original didn't filter by tenant (e.g. the `update_cog` ancestor walk,
or the service-callback `Cog` lookups keyed only by id), this module
preserves that exactly rather than "fixing" it (see migration rules #10).
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


# =============================================================================
# update_cog — ancestor-cycle-detection walk (also reused by
# create_workflow_output_folder's input_cog lookup — same shape).
# =============================================================================


def get_cog_active_by_id(db: Session, cog_id: int) -> Optional[models.Cog]:
    """A non-deleted cog by id, with NO tenant filter — matches the
    pre-migration `update_cog` ancestor-chain walk exactly (the walk only
    ever visits ids reachable from a caller-supplied, not-yet-tenant-
    validated `parent_id`, so this must not silently add a filter the
    original didn't have)."""
    return db.query(models.Cog).filter(
        models.Cog.id == cog_id,
        models.Cog.deleted_at.is_(None),
    ).first()


# =============================================================================
# delete_cog
# =============================================================================


def get_active_files_for_cog(db: Session, cog_id: int) -> List[models.File]:
    """Active (non-deleted) File rows for a cog — the set `delete_cog`
    soft-deletes and decrements tenant storage for."""
    return db.query(models.File).filter(
        models.File.cog_id == cog_id,
        models.File.deleted_at.is_(None),
    ).all()


def get_tenant_by_id(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    """Plain tenant lookup — shared by `delete_cog` (storage decrement),
    `restore_cog` (storage re-credit), and `upload_file` (storage
    increment)."""
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


# =============================================================================
# restore_cog / set_thumbnail — bypass the soft-delete filter on purpose
# =============================================================================


def get_cog_by_id_and_tenant_any_delete_state(
    db: Session, *, cog_id: int, tenant_id: int
) -> Optional[models.Cog]:
    """Tenant-scoped cog lookup that does NOT exclude soft-deleted rows.

    `restore_cog` needs this to find the very row it's about to un-delete
    (the ordinary `crud.get_cog_by_id_and_tenant` filters `deleted_at IS
    NULL` and would never find it). `set_thumbnail`'s service callback used
    the same unfiltered-by-delete-state shape pre-migration; ported as-is.
    """
    return db.query(models.Cog).filter(
        models.Cog.id == cog_id,
        models.Cog.tenant_id == tenant_id,
    ).first()


def get_deleted_files_for_cog(db: Session, cog_id: int) -> List[models.File]:
    """Soft-deleted File rows for a cog — what `restore_cog` un-deletes and
    re-credits to tenant storage."""
    return db.query(models.File).filter(
        models.File.cog_id == cog_id,
        models.File.deleted_at.isnot(None),
    ).all()


# =============================================================================
# ContentBlob lookups — the single most-repeated shape in this file
# (multipart-complete, check-by-hash, from-hash, upload-file,
# upload-processed-file, upload-text-file, register-uploaded-file,
# create-preview, download_file, stream_file).
# =============================================================================


def get_content_blob_by_id_and_tenant(
    db: Session, *, blob_id: str, tenant_id: int
) -> Optional[models.ContentBlob]:
    return db.query(models.ContentBlob).filter(
        models.ContentBlob.id == blob_id,
        models.ContentBlob.tenant_id == tenant_id,
    ).first()


def count_versions_for_cog(db: Session, cog_id: int) -> int:
    """Version count for a cog — used to compute the next `version_number`."""
    from sqlalchemy import func as sa_func

    return db.query(sa_func.count(models.Version.id)).filter(
        models.Version.cog_id == cog_id,
    ).scalar() or 0


# =============================================================================
# create_workflow_output_folder
# =============================================================================


def get_workflow_run_by_id(db: Session, workflow_run_id: int):
    """Unscoped WorkflowRun lookup — ports the pre-migration
    `create_workflow_output_folder` no-input-cog branch exactly (it derives
    `tenant_id` FROM this row, so it can't filter by tenant first)."""
    from models import WorkflowRun

    return db.query(WorkflowRun).filter(WorkflowRun.id == workflow_run_id).first()


def get_active_cog_by_name_parent_tenant(
    db: Session, *, name: str, parent_id: Optional[int], tenant_id: int
) -> Optional[models.Cog]:
    """Idempotency check: does an output folder with this name already exist
    under this parent (same workflow run re-executing)?"""
    return db.query(models.Cog).filter(
        models.Cog.name == name,
        models.Cog.parent_id == parent_id,
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
    ).first()


# =============================================================================
# upload_processed_file / upload_text_file / create_preview — output-folder
# lookup shape shared by all three.
# =============================================================================


def get_active_cog_by_id_and_tenant(
    db: Session, *, cog_id: int, tenant_id: int
) -> Optional[models.Cog]:
    return db.query(models.Cog).filter(
        models.Cog.id == cog_id,
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
    ).first()


def get_active_subfolder_by_name_parent_tenant(
    db: Session, *, name: str, parent_id: int, tenant_id: int
) -> Optional[models.Cog]:
    """`upload_processed_file`'s node-subfolder lookup — same shape as
    `get_active_cog_by_name_parent_tenant` plus a `cog_type == ASSET_FOLDER`
    constraint, kept separate so neither caller has to over-filter."""
    return db.query(models.Cog).filter(
        models.Cog.name == name,
        models.Cog.parent_id == parent_id,
        models.Cog.cog_type == models.CogType.ASSET_FOLDER,
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
    ).first()


def get_workflow_run_by_id_and_tenant(db: Session, *, workflow_run_id: int, tenant_id: int):
    """`upload_text_file`'s folder-less staging branch — tenant-scoped,
    unlike `get_workflow_run_by_id` above (that one can't be, since it's
    the source of the tenant id)."""
    from models import WorkflowRun

    return db.query(WorkflowRun).filter(
        WorkflowRun.id == workflow_run_id,
        WorkflowRun.tenant_id == tenant_id,
    ).first()


# =============================================================================
# Service callbacks (set_thumbnail reuses get_cog_by_id_and_tenant_any_delete_state
# above; sldprt/pointcloud callbacks use this unscoped-by-tenant shape) +
# converted-step / converted-pointcloud polling.
# =============================================================================


def get_cog_by_id_unscoped(db: Session, cog_id: int) -> Optional[models.Cog]:
    """Cog lookup with NO tenant filter at all — matches the pre-migration
    `sldprt_conversion_callback` / `pointcloud_conversion_callback` shape.
    Both callbacks immediately pin the caller's `run_task` token to
    `cog.tenant_id` afterward (`pin_run_task_tenant`), which is where the
    tenant check actually happens for that principal type — preserved
    as-is, not "fixed" here (migration rules #10)."""
    return db.query(models.Cog).filter(models.Cog.id == cog_id).first()


def get_latest_task_by_node_id(db: Session, node_id: str):
    """Most recent ApiTaskQueue row for a given synthetic `node_id` (e.g.
    `sldprt-convert-{cog_id}` / `pointcloud-preview-{cog_id}`) — used by the
    stuck-pending heal check in `get_converted_step` / `get_converted_pointcloud`."""
    from models import ApiTaskQueue

    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.node_id == node_id,
    ).order_by(ApiTaskQueue.id.desc()).first()


def get_version_by_id(db: Session, version_id: int) -> Optional[models.Version]:
    return db.query(models.Version).filter(models.Version.id == version_id).first()


# =============================================================================
# download_file / stream_file
# =============================================================================


def get_active_file_for_cog_and_tenant(
    db: Session, *, cog_id: int, tenant_id: int
) -> Optional[models.File]:
    return db.query(models.File).filter(
        models.File.cog_id == cog_id,
        models.File.tenant_id == tenant_id,
        models.File.deleted_at.is_(None),
    ).first()


# =============================================================================
# create_download_job / update_download_job_progress
# =============================================================================


def count_active_file_children(db: Session, parent_cog_id: int) -> int:
    """Rough per-folder file count used for the download job's initial
    `total_files` progress estimate."""
    from sqlalchemy import func as sa_func

    return db.query(sa_func.count(models.Cog.id)).filter(
        models.Cog.parent_id == parent_cog_id,
        models.Cog.cog_type == models.CogType.FILE,
        models.Cog.deleted_at.is_(None),
    ).scalar() or 0


def get_download_job_by_id(db: Session, job_id: str):
    return db.query(models.DownloadJob).filter(
        models.DownloadJob.id == job_id,
    ).first()
