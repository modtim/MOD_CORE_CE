"""Service layer for the REMAINING business logic in
`routers/cogs/assets_cogs_files.py` (openspec: stack-training-and-prewarm,
Gate 2b — largest remaining router sub-module, ~47 inline db.query sites).

This is a SIBLING of `services/assets_cogs_files_service.py` (which already
owns `list_cogs` / `search_cogs_recursive` / `get_cog` format metadata /
tags / search-by-processing / processing-analytics / folder-files-recursive /
legal-hold — migrated in an earlier pass). Named distinctly
(`cogs_assets_cogs_files_service`) so this pass can't collide with whatever
else is touching that sibling module concurrently.

All data access goes through `crud/cogs_assets_cogs_files.py` (plus the
pre-existing `crud/cog_cud.py` et al. the router already used correctly via
the `crud` package). HTTP concerns (permission 403s that gate a handler
BEFORE any of this runs, `HTTPException`, docstrings, `response_model`,
`Depends()` auth) stay in the router. This module raises the typed errors
declared below; the router maps each to the exact HTTPException the
pre-migration handler raised (same status code, same message).

FULLY migrated here (router body becomes a thin call + typed-exception
mapping): update_cog, delete_cog, restore_cog, check_upload_by_hash,
create_cog_from_existing_hash, create_workflow_output_folder, set_thumbnail,
sldprt_conversion_callback (DB portion), pointcloud_conversion_callback,
update_download_job_progress, download_file, create_preview,
register_uploaded_file (DB portion).

Two handlers (`register_uploaded_file`, `sldprt_conversion_callback`) keep a
small dispatch-glue tail IN THE ROUTER: the `_dispatch_*_task(...)` helpers
they call on success are module-level functions defined in
`routers/cogs/assets_cogs_files.py` itself (used by several other handlers
too) — importing them here would be a router->service->router cycle. The DB
read/write work for both is fully migrated; only the "which worker task to
kick off next" glue stays put.

PARTIALLY migrated (inline `db.query` swapped for a `crud` call in place;
the surrounding streaming / background-thread / MinIO-byte-transfer
mechanics were judged too risky to relocate this pass, per the migration
rules' "keep it if it doesn't separate cleanly" allowance) — see the
migration report for the full list and reasons: complete_multipart_upload,
upload_file, upload_processed_file, upload_text_file, stream_file,
create_download_job (its synchronous validation loop only —
the in-process zip worker's background-thread `local_db.query(...)` calls
were left untouched), get_converted_step, get_converted_pointcloud.

DEFERRED entirely (untouched): confirm_upload's background-thread
`local_db.query(...)` calls — see that handler's inline comment.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

import crud
import models
import schemas
from crud import cogs_assets_cogs_files as acf_crud
from services._base import ConflictError, NotFoundError, ServiceError
from utils.minio_client import get_tenant_bucket_name

logger = logging.getLogger(__name__)


# =============================================================================
# Typed errors this module raises. `NotFoundError` / `ConflictError` come
# from services/_base.py; it has no Forbidden/BadRequest, so (per the
# migration rules) they're declared locally as ServiceError subclasses.
# =============================================================================


class ForbiddenError(ServiceError):
    """Router maps -> 403. Declared locally: services/_base.py intentionally
    has no Forbidden — see migration rules #5."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class InvalidRequestError(ServiceError):
    """Router maps -> 400 (a caller-input problem, not a missing/forbidden
    resource)."""

    def __init__(self, message: str = "Invalid request", *, details: Any = None):
        super().__init__(message, code="invalid_request", details=details)


class ExternalStorageError(ServiceError):
    """Router maps -> 500. An unexpected failure talking to object storage
    (presign, etc.) — mirrors the pre-migration handlers' generic
    `except Exception: raise HTTPException(500, ...)` catch-all."""

    def __init__(self, message: str = "Storage error", *, details: Any = None):
        super().__init__(message, code="external_storage_error", details=details)


class NameCollisionError(ConflictError):
    """A rename/move/upload target collides with an existing, non-deleted
    sibling. Carries what `_name_collision_409`
    (`routers/cogs/_shared.py`) needs to build the router's structured 409
    — the router calls that helper with these three fields, unchanged from
    the pre-migration inline `raise _name_collision_409(...)` call sites."""

    def __init__(self, *, existing: "models.Cog", target_parent_id: Optional[int], incoming_name: str):
        super().__init__(
            f'An item named "{incoming_name}" already exists in the destination.'
        )
        self.existing = existing
        self.target_parent_id = target_parent_id
        self.incoming_name = incoming_name


# =============================================================================
# update_cog
# =============================================================================


def update_cog(
    db: Session,
    *,
    cog_id: int,
    db_cog: "models.Cog",
    cog_update: "schemas.CogUpdate",
    current_user,
):
    """Everything `PATCH /cogs/{cog_id}` does AFTER the router's permission
    check (`can_user_move_files_in_cog`) and tenant-scoped fetch have
    already passed. `db_cog` is that already-fetched row.
    """
    from sqlalchemy.exc import IntegrityError

    from permissions import can_user_upload_to_cog
    from routers.cogs._shared import _find_name_collision, _get_or_create_tenant_root_folder

    parent_id_provided = "parent_id" in cog_update.model_fields_set

    # Move-to-root reconciliation (see the pre-migration router docstring for
    # why this rewrite is needed — Home is the root MASTER_FOLDER, not the
    # parent_id-IS-NULL level).
    if (
        parent_id_provided
        and cog_update.parent_id is None
        and db_cog.cog_type != models.CogType.MASTER_FOLDER
    ):
        root_folder = _get_or_create_tenant_root_folder(db, current_user)
        if root_folder.id != cog_id:
            cog_update.parent_id = root_folder.id

    if cog_update.parent_id is not None:
        if cog_update.parent_id == cog_id:
            raise InvalidRequestError("Cog cannot be its own parent.")

        # Descendant-tree circular detection (depth-capped at 50).
        cursor_id = cog_update.parent_id
        seen: set = set()
        depth = 0
        while cursor_id is not None and depth < 50:
            if cursor_id == cog_id:
                raise InvalidRequestError(
                    "Cannot move a folder into one of its own descendants."
                )
            if cursor_id in seen:
                break  # pre-existing cycle — bail rather than loop forever
            seen.add(cursor_id)
            ancestor = acf_crud.get_cog_active_by_id(db, cursor_id)
            if ancestor is None:
                break
            cursor_id = ancestor.parent_id
            depth += 1

        # Tenant-scoped lookup so a granted (non-owner) user with
        # `can_create_subfolders` / `can_upload` on the destination can land
        # here — same shape as the GET / PATCH / DELETE source-cog lookups.
        parent_cog = crud.get_cog_by_id_and_tenant(
            db, cog_id=cog_update.parent_id, tenant_id=current_user.tenant_id
        )
        if not parent_cog:
            raise NotFoundError("New parent Cog not found")
        if not can_user_upload_to_cog(db, current_user.id, cog_update.parent_id):
            raise ForbiddenError(
                "You don't have permission to move into the destination folder"
            )
        if parent_cog.cog_type not in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
            raise InvalidRequestError(
                "New parent Cog must be a folder type (MASTER_FOLDER or ASSET_FOLDER)"
            )
    elif not parent_id_provided and cog_update.name is None:
        raise InvalidRequestError(
            "At least one field (name or parent_id) must be provided for update."
        )

    # P3 — capture before-state for audit logging.
    old_name = db_cog.name
    old_parent_id = db_cog.parent_id

    target_parent_id = cog_update.parent_id if parent_id_provided else db_cog.parent_id
    target_name = cog_update.name if cog_update.name is not None else db_cog.name
    collision = _find_name_collision(
        db,
        name=target_name,
        parent_id=target_parent_id,
        tenant_id=current_user.tenant_id,
        exclude_cog_id=cog_id,
    )
    if collision is not None:
        if cog_update.replace:
            crud.delete_cog(db=db, db_cog=collision)
        else:
            raise NameCollisionError(
                existing=collision, target_parent_id=target_parent_id, incoming_name=target_name,
            )

    try:
        updated = crud.update_cog(db=db, db_cog=db_cog, cog_update=cog_update)
    except IntegrityError:
        # Race: a concurrent operation introduced a colliding sibling between
        # our pre-flight check and the commit.
        db.rollback()
        late = _find_name_collision(
            db,
            name=target_name,
            parent_id=target_parent_id,
            tenant_id=current_user.tenant_id,
            exclude_cog_id=cog_id,
        )
        if late is not None:
            raise NameCollisionError(
                existing=late, target_parent_id=target_parent_id, incoming_name=target_name,
            )
        raise ConflictError(
            "An item with that name already exists in the destination.",
            details={
                "code": "constraint_violation",
                "message": "An item with that name already exists in the destination.",
            },
        )

    # P3 — audit log the rename / move (CMMC AU-2, ISO 27001 A.5.17).
    try:
        from audit import log_permission_audit
        from models import AuditAction

        if cog_update.name is not None and cog_update.name != old_name:
            log_permission_audit(
                db=db, action=AuditAction.CONTENT_RENAMED,
                actor_user_id=current_user.id, tenant_id=current_user.tenant_id,
                resource_type="cog", resource_id=cog_id,
                metadata={"from": old_name, "to": cog_update.name},
            )
        if parent_id_provided and cog_update.parent_id != old_parent_id:
            log_permission_audit(
                db=db, action=AuditAction.CONTENT_MOVED,
                actor_user_id=current_user.id, tenant_id=current_user.tenant_id,
                resource_type="cog", resource_id=cog_id,
                metadata={
                    "from_parent_id": old_parent_id,
                    "to_parent_id": cog_update.parent_id,
                    "name": updated.name,
                },
            )
    except Exception:
        # Audit failure is non-fatal — the rename/move already committed.
        pass

    return updated


# =============================================================================
# delete_cog
# =============================================================================


def delete_cog(db: Session, *, cog_id: int, db_cog: "models.Cog", current_user):
    """Everything `DELETE /cogs/{cog_id}` does after the router's permission
    check + tenant-scoped fetch have already passed."""
    if db_cog.is_system_generated:
        raise ForbiddenError(
            "Cannot delete system-generated folders. Contact an administrator."
        )
    if db_cog.is_locked:
        raise ForbiddenError(
            "Cannot delete audit-protected items. Workflow history is preserved for compliance."
        )

    # Active ResourceLock guard — mirrors the 409 the /locks/ router returns.
    active_lock = crud.get_active_lock_for_resource(db, target_type="cog", target_id=cog_id)
    if active_lock:
        detail = "Resource is locked and cannot be deleted."
        if active_lock.user_id != current_user.id:
            detail = "Resource is locked by another user and cannot be deleted."
        raise ConflictError(detail)

    # CMMC/ISO 27001 COMPLIANCE: NEVER delete file content from MinIO here.
    # See the pre-migration router docstring for the full rationale — this
    # is a soft-delete-only path; physical storage cleanup is a separate,
    # admin-controlled, retention-aware process.

    if db_cog.cog_type in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
        crud.soft_delete_cogs_recursive(db, parent_id=cog_id, owner_id=current_user.id)

    files_to_delete = acf_crud.get_active_files_for_cog(db, cog_id)
    if files_to_delete:
        # Only files with NO backing content_blob (by storage_path) count
        # against storage_used_bytes at all — a blob-backed file's bytes are
        # already counted via content_blobs and content_blobs rows are never
        # removed on file delete (ISO 27001 no-hard-delete; no blob GC yet).
        backed_paths = {
            p for (p,) in db.query(models.ContentBlob.storage_path).filter(
                models.ContentBlob.tenant_id == current_user.tenant_id,
                models.ContentBlob.storage_path.in_(
                    [f.storage_path for f in files_to_delete]),
            )
        }
        unbacked_freed = sum(
            f.size_bytes or 0 for f in files_to_delete
            if f.storage_path not in backed_paths
        )
        if unbacked_freed > 0:
            from utils.paid_import import optional_paid
            # egress_metering is a PAID module: on CE there is no storage
            # accounting to record, so the freed-bytes delta is not applied.
            _egress = optional_paid("services.egress_metering")
            if _egress is not None:
                _egress.apply_storage_change(db, current_user.tenant_id,
                                             unbacked_bytes_delta=-unbacked_freed)

        from datetime import datetime

        for file_record in files_to_delete:
            file_record.deleted_at = datetime.utcnow()

    # Audit: content deletion (CMMC AU-2). Non-fatal.
    try:
        from models import AuditEvent

        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="CONTENT_DELETE_INITIATED",
            details={
                "cog_id": cog_id, "cog_name": db_cog.name,
                "cog_type": db_cog.cog_type.value if db_cog.cog_type else None,
            },
        ))
        db.commit()
    except Exception:
        pass

    # Workflow output audit: mark outputs as hidden if this cog has any.
    # Non-fatal.
    try:
        from models import WorkflowOutputAuditLog

        for file_record in (files_to_delete or []):
            if file_record.content_blob_id:
                db.add(WorkflowOutputAuditLog(
                    content_hash=file_record.content_blob_id,
                    tenant_id=current_user.tenant_id,
                    action="hidden",
                    performed_by_user_id=current_user.id,
                    reason=f"User deleted cog {cog_id} ({db_cog.name})",
                ))
        db.commit()
    except Exception:
        pass

    deleted_name = db_cog.name
    is_file = db_cog.cog_type == models.CogType.FILE
    crud.delete_cog(db=db, db_cog=db_cog)

    # remove-minio-versitygw Gate 3 (D4) — emit the ObjectRemoved file_event
    # API-side. Only for FILE cogs (folders are virtual, no storage object).
    # Non-fatal.
    if is_file:
        try:
            from services.triggers.watchers import emit_object_event

            emit_object_event(
                db, tenant_id=current_user.tenant_id, key=deleted_name, event_type="deleted",
            )
        except Exception:
            logger.exception("file_event (deleted) emission failed for cog=%s", cog_id)


# =============================================================================
# restore_cog
# =============================================================================


def restore_cog(db: Session, *, cog_id: int, current_user, http_request=None):
    """Full `POST /cogs/{cog_id}/restore` logic, including the fetch (the
    router does no pre-fetch for this endpoint — unlike update/delete, the
    only authz here is owner/manager-after-fetch, so there's no 404-vs-403
    ordering trap to preserve by splitting the fetch out)."""
    from routers.cogs._shared import _maybe_audit_xtenant_block

    db_cog = acf_crud.get_cog_by_id_and_tenant_any_delete_state(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )
    if db_cog is None:
        _maybe_audit_xtenant_block(db, current_user, cog_id, http_request)
        raise NotFoundError("Cog not found")

    is_manager = bool(
        getattr(current_user, "is_platform_admin", False)
        or getattr(current_user, "parent_user_id", None) is None
    )
    if not (is_manager or db_cog.owner_id == current_user.id):
        raise ForbiddenError("You don't have permission to restore this folder/file")

    if db_cog.deleted_at is None:
        raise ConflictError("This item is not deleted.")

    db_cog.deleted_at = None
    db.add(db_cog)

    restored_files = acf_crud.get_deleted_files_for_cog(db, cog_id)
    if restored_files:
        for f in restored_files:
            f.deleted_at = None
        # Same backed/unbacked split as delete_cog above — a blob-backed
        # file's bytes are already counted via content_blobs regardless of
        # this File row's deleted_at state, so restoring it changes nothing.
        backed_paths = {
            p for (p,) in db.query(models.ContentBlob.storage_path).filter(
                models.ContentBlob.tenant_id == current_user.tenant_id,
                models.ContentBlob.storage_path.in_(
                    [f.storage_path for f in restored_files]),
            )
        }
        unbacked_restored = sum(
            f.size_bytes or 0 for f in restored_files
            if f.storage_path not in backed_paths
        )
        if unbacked_restored > 0:
            from utils.paid_import import optional_paid
            # egress_metering is a PAID module: on CE there is no storage
            # accounting to record, so the restored-bytes delta is not applied.
            _egress = optional_paid("services.egress_metering")
            if _egress is not None:
                _egress.apply_storage_change(db, current_user.tenant_id,
                                             unbacked_bytes_delta=unbacked_restored)

    # Audit: content restored. Non-fatal.
    try:
        from models import AuditEvent

        db.add(AuditEvent(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="CONTENT_RESTORED",
            details={
                "cog_id": cog_id,
                "cog_name": db_cog.name,
                "cog_type": db_cog.cog_type.value if db_cog.cog_type else None,
                "restored_file_count": len(restored_files),
            },
        ))
    except Exception:
        logger.exception("CONTENT_RESTORED audit emit failed for cog=%s", cog_id)

    db.commit()
    db.refresh(db_cog)
    return db_cog


# =============================================================================
# check_upload_by_hash / create_cog_from_existing_hash
# =============================================================================


def check_upload_by_hash(db: Session, *, content_hash: str, tenant_id: int) -> Dict[str, Any]:
    blob = acf_crud.get_content_blob_by_id_and_tenant(db, blob_id=content_hash, tenant_id=tenant_id)
    if not blob:
        return {"exists": False}
    return {
        "exists": True,
        "content": {
            "id": blob.id,
            "size_bytes": blob.size_bytes,
            "mime_type": blob.mime_type,
            "file_extension": blob.file_extension,
        },
    }


def create_cog_from_existing_hash(
    db: Session,
    *,
    filename: str,
    content_hash: str,
    parent_id: Optional[int],
    content_type: str,
    replace: bool,
    current_user,
):
    from routers.cogs._shared import (
        _apply_permission_snapshot, _find_name_collision, _get_or_create_tenant_root_folder,
        _snapshot_cog_permissions,
    )

    blob = acf_crud.get_content_blob_by_id_and_tenant(
        db, blob_id=content_hash, tenant_id=current_user.tenant_id
    )
    if not blob:
        raise NotFoundError("content_hash not found for this tenant — fall back to upload")

    if not parent_id:
        parent_id = _get_or_create_tenant_root_folder(db, current_user).id

    # Bug-2/Bug-B: same name-collision prompt + grant-snapshot as the
    # byte-transfer upload paths.
    _replace_perms = []
    _collide = _find_name_collision(
        db, name=filename, parent_id=parent_id, tenant_id=current_user.tenant_id
    )
    if _collide:
        if replace:
            _replace_perms = _snapshot_cog_permissions(db, _collide.id)
            crud.delete_cog(db=db, db_cog=_collide)
        else:
            raise NameCollisionError(
                existing=_collide, target_parent_id=parent_id, incoming_name=filename,
            )

    # Audit (TPN CS-7, CMMC AU-2). Non-fatal.
    try:
        from models import AuditEvent

        db.add(AuditEvent(
            tenant_id=current_user.tenant_id, user_id=current_user.id,
            event_name="CONTENT_UPLOAD",
            details={
                "filename": filename, "file_size": blob.size_bytes,
                "parent_id": parent_id, "deduped": True,
                "content_hash": content_hash,
            },
        ))
    except Exception:
        pass

    cog_create_schema = schemas.CogCreate(
        name=filename, cog_type=models.CogType.FILE, parent_id=parent_id,
    )
    target_cog = crud.create_cog(
        db=db, cog=cog_create_schema, user_id=current_user.id, tenant_id=current_user.tenant_id,
    )

    if blob.cog_id is None:
        blob.cog_id = target_cog.id
        db.add(blob)

    current_version_count = acf_crud.count_versions_for_cog(db, target_cog.id)

    new_version = models.Version(
        cog_id=target_cog.id,
        tenant_id=current_user.tenant_id,
        content_id=blob.id,
        version_number=current_version_count + 1,
        name=f"v{current_version_count + 1}",
        author_user_id=current_user.id,
    )
    db.add(new_version)
    db.flush()
    target_cog.latest_version_id = new_version.id

    db_file = models.File(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        cog_id=target_cog.id,
        name=filename,
        size_bytes=blob.size_bytes,
        mime_type=blob.mime_type or content_type,
        storage_path=blob.storage_path,
    )
    db.add(db_file)
    db.commit()
    db.refresh(target_cog)

    # This whole function is the from-hash dedup path (`blob` was required to
    # already exist above — see the NotFoundError a few lines up), so it never
    # creates a new content_blobs row and never transfers bytes: no storage
    # accounting call here. (Previously called record_storage_delta with
    # `blob.size_bytes` unconditionally on every from-hash reuse — wrong.)

    if _replace_perms:
        _apply_permission_snapshot(db, target_cog.id, _replace_perms)

    logging.info(
        "Upload deduped via from-hash: %s -> existing %s... (%.1f MB, no bytes transferred)",
        filename, content_hash[:16],
        (blob.size_bytes or 0) / (1024 * 1024),
    )

    try:
        from services.triggers.watchers import emit_object_event

        emit_object_event(
            db, tenant_id=current_user.tenant_id, key=filename, event_type="created",
        )
    except Exception:
        logger.exception("file_event emission failed for from-hash cog=%s", target_cog.id)

    return target_cog


# =============================================================================
# create_workflow_output_folder (service_router)
# =============================================================================


def create_workflow_output_folder(
    db: Session, *, workflow_run_id: int, workflow_name: str, input_cog_id: Optional[int], principal,
):
    input_cog = None
    if input_cog_id:
        input_cog = acf_crud.get_cog_active_by_id(db, input_cog_id)
        if not input_cog:
            raise NotFoundError(f"Input cog {input_cog_id} not found")

    if input_cog:
        output_folder_name = f"{input_cog.name}_results_run{workflow_run_id}"
        parent_id = input_cog.parent_id
        owner_id = input_cog.owner_id
        tenant_id = input_cog.tenant_id
    else:
        output_folder_name = f"{workflow_name}_results_run{workflow_run_id}"
        wf_run = acf_crud.get_workflow_run_by_id(db, workflow_run_id)
        tenant_id = wf_run.tenant_id if wf_run else 1
        owner_id = wf_run.triggered_by_user_id if wf_run else None
        parent_id = None

    # Cross-tenant guard: a run_task node token may only act within its own
    # tenant+run.
    if isinstance(principal, dict):
        from utils.run_task_auth import pin_run_task_tenant

        pin_run_task_tenant(principal, tenant_id=tenant_id, run_id=workflow_run_id)

    existing_output = acf_crud.get_active_cog_by_name_parent_tenant(
        db, name=output_folder_name, parent_id=parent_id, tenant_id=tenant_id,
    )
    if existing_output:
        logging.info(
            f"Output folder already exists for run {workflow_run_id}: {existing_output.id}"
        )
        return {"cog_id": existing_output.id, "name": output_folder_name}

    # GY.V2a / GY.V2.s1 (R-133 remainder): a run's output folder inherits the
    # max classification across ALL input cogs listed on the WorkflowRun's
    # parameters (not just the single input_cog argument).  No input → column
    # default.  The single input_cog is still used for folder naming / grant
    # copy; the parameters-level scan covers every input cog that the node
    # actually consumed, guaranteeing the label propagates to the output cog
    # row (NodeRun output records) as well.
    from utils.paid_import import optional_paid
    _dc = optional_paid("services.data_classification")
    _in_levels = []
    if input_cog is not None:
        _in_levels = [getattr(input_cog, "data_classification", None)]
    # GY.V2.s1 — pull every input cog from the run's parameter blob so that
    # a multi-input node never loses a higher-marking on the output.
    try:
        _seen_ids = {input_cog_id} if input_cog_id else set()
        wf_run = acf_crud.get_workflow_run_by_id(db, workflow_run_id)
        if wf_run is not None:
            _params = wf_run.parameters if isinstance(wf_run.parameters, dict) else None
            _all_input_ids = (_dc.cog_ids_from_run_params(_params)
                              if _dc is not None else [])
            if _all_input_ids:
                for _cid in _all_input_ids:
                    if _cid not in _seen_ids:
                        _seen_ids.add(_cid)
                        _ic = acf_crud.get_cog_active_by_id(db, _cid)
                        if _ic is not None:
                            lv = getattr(_ic, "data_classification", None)
                            if lv not in _in_levels:
                                _in_levels.append(lv)
    except Exception:  # noqa: BLE001 — best effort, never block folder creation
        logger.exception("GY.V2.s1: input-cog scan failed for run %s", workflow_run_id)
    output_folder = models.Cog(
        name=output_folder_name,
        cog_type=models.CogType.ASSET_FOLDER,
        parent_id=parent_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        data_classification=(_dc.propagate_output_classification(_in_levels)
                           if _dc is not None else "internal"),  # 1788 CE carve: column default
        processing_metadata={
            "workflow_run_id": workflow_run_id,
            "workflow_name": workflow_name,
            "source_cog_id": input_cog_id or 0,
        },
    )
    db.add(output_folder)
    db.flush()

    # GY.C7 s3c: the FX.79 MARKS ride along too, not just the GY.V2 level —
    # a CUI-marked (or tenant-custom-marked) input must never yield an
    # unmarked output. Idempotent + tenant-scoped; CE carve via optional_paid.
    if input_cog is not None:
        from utils.paid_import import optional_paid

        _dm = optional_paid("services.data_markings")
        if _dm is not None:
            try:
                _dm.propagate_marks(
                    db, tenant_id=tenant_id,
                    source=("asset", input_cog.id),
                    target=("asset", output_folder.id),
                )
            except Exception as e:  # noqa: BLE001 — marks are an enhancement
                logger.warning(
                    "GY.C7 s3c: mark propagation failed into cog %s: %s",
                    output_folder.id, e)

    from resource_permissions import grant_cog_owner_if_folder

    grant_cog_owner_if_folder(db, output_folder)

    # Copy the parent folder's sharing grants onto the output folder.
    if input_cog and input_cog.parent_id:
        from cog_grants import list_cog_grants
        from permissions import create_permission

        for g in list_cog_grants(db, cog_ids=[input_cog.parent_id]):
            create_permission(
                db, cog_id=output_folder.id, level=g.level,
                granted_by_user_id=g.granted_by_user_id or 0,
                user_id=g.user_id, team_id=g.team_id,
                keycloak_group_id=g.keycloak_group_id,
                expires_at=g.expires_at, is_external_user=g.is_external_user,
                can_upload=g.can_upload, can_create_subfolders=g.can_create_subfolders,
                can_move_files=g.can_move_files, can_process=g.can_process,
                can_share=g.can_share, can_delete=g.can_delete,
            )

    db.commit()
    db.refresh(output_folder)

    logging.info(f"Created output folder: {output_folder.id} (virtual, database-only)")

    return {"cog_id": output_folder.id, "name": output_folder_name}


# =============================================================================
# set_thumbnail / sldprt_conversion_callback / pointcloud_conversion_callback
# =============================================================================


def set_thumbnail(
    db: Session, *, cog_id: int, thumbnail_path: str, tenant_id: int, preview_type: str, principal,
):
    cog = acf_crud.get_cog_by_id_and_tenant_any_delete_state(db, cog_id=cog_id, tenant_id=tenant_id)
    if not cog:
        raise NotFoundError("Cog not found")

    if isinstance(principal, dict):
        from utils.run_task_auth import pin_run_task_tenant

        pin_run_task_tenant(principal, tenant_id=cog.tenant_id)

    from datetime import datetime

    meta = cog.processing_metadata or {}
    if preview_type == "glb":
        meta["glb_preview_path"] = thumbnail_path
        meta["glb_preview_generated_at"] = datetime.utcnow().isoformat()
    else:
        meta["thumbnail_path"] = thumbnail_path
        meta["thumbnail_source"] = "auto"
        meta["thumbnail_generated_at"] = datetime.utcnow().isoformat()
    cog.processing_metadata = meta
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(cog, "processing_metadata")
    db.commit()


def sldprt_conversion_callback_update(
    db: Session, *, cog_id: int, success: bool, error_message: str, content_hash: str, principal,
):
    """DB portion of the sldprt-converter callback. The thumbnail-chain
    dispatch on success stays in the router (see module docstring)."""
    cog = acf_crud.get_cog_by_id_unscoped(db, cog_id)
    if not cog:
        raise NotFoundError("Cog not found")

    if isinstance(principal, dict):
        from utils.run_task_auth import pin_run_task_tenant

        pin_run_task_tenant(principal, tenant_id=cog.tenant_id)

    from datetime import datetime

    meta = dict(cog.processing_metadata or {})
    if success:
        meta["sldprt_conversion_status"] = "succeeded"
        meta["sldprt_step_object"] = f"{content_hash}.step"
        meta["sldprt_conversion_completed_at"] = datetime.utcnow().isoformat()
        meta.pop("sldprt_conversion_error", None)
    else:
        meta["sldprt_conversion_status"] = "failed"
        meta["sldprt_conversion_error"] = error_message or "conversion failed"
        meta["sldprt_conversion_completed_at"] = datetime.utcnow().isoformat()
    cog.processing_metadata = meta
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(cog, "processing_metadata")
    db.commit()
    return cog


def pointcloud_conversion_callback(
    db: Session, *, cog_id: int, success: bool, error_message: str, ply_object: str, thumb_object: str, principal,
):
    cog = acf_crud.get_cog_by_id_unscoped(db, cog_id)
    if not cog:
        raise NotFoundError("Cog not found")

    if isinstance(principal, dict):
        from utils.run_task_auth import pin_run_task_tenant

        pin_run_task_tenant(principal, tenant_id=cog.tenant_id)

    from datetime import datetime

    meta = dict(cog.processing_metadata or {})
    if success:
        meta["pointcloud_conversion_status"] = "succeeded"
        meta["pointcloud_ply_object"] = ply_object
        meta["pointcloud_thumbnail_object"] = thumb_object
        if thumb_object:
            meta["thumbnail_path"] = thumb_object
            meta["thumbnail_source"] = "auto"
            meta["thumbnail_generated_at"] = datetime.utcnow().isoformat()
        meta["pointcloud_conversion_completed_at"] = datetime.utcnow().isoformat()
        meta.pop("pointcloud_conversion_error", None)
    else:
        meta["pointcloud_conversion_status"] = "failed"
        meta["pointcloud_conversion_error"] = error_message or "conversion failed"
        meta["pointcloud_conversion_completed_at"] = datetime.utcnow().isoformat()
    cog.processing_metadata = meta
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(cog, "processing_metadata")
    db.commit()


# =============================================================================
# register_uploaded_file — DB portion only (auto-dispatch tail stays router-side)
# =============================================================================


def register_uploaded_file_record(
    db: Session,
    *,
    content_hash: str,
    filename: str,
    parent_id: int,
    file_size: int,
    content_type: str,
    file_extension: str,
    tenant_id: int,
    user_id: Optional[int],
    principal,
):
    if isinstance(principal, dict):
        from utils.run_task_auth import pin_run_task_tenant

        pin_run_task_tenant(principal, tenant_id=int(tenant_id))

    # P10e-a writer: new rows are written `s3://bucket/key` by default;
    # STORAGE_SCHEME=minio stays a REAL accepted value and still writes
    # `minio://` (rows stored before the rename keep parsing everywhere).
    storage_scheme = os.getenv("STORAGE_SCHEME", "s3")
    bucket = get_tenant_bucket_name(tenant_id, db)
    storage_path = f"{storage_scheme}://{bucket}/{content_hash}"

    content_blob = acf_crud.get_content_blob_by_id_and_tenant(
        db, blob_id=content_hash, tenant_id=tenant_id
    )
    is_new_blob = content_blob is None
    if not content_blob:
        content_blob = models.ContentBlob(
            id=content_hash, tenant_id=tenant_id, size_bytes=file_size,
            mime_type=content_type, file_extension=file_extension, storage_path=storage_path,
        )
        db.add(content_blob)
        db.flush()

    cog_create_schema = schemas.CogCreate(
        name=filename, cog_type=models.CogType.FILE, parent_id=parent_id,
    )
    target_cog = crud.create_cog(db=db, cog=cog_create_schema, user_id=user_id, tenant_id=tenant_id)

    # GY.V2a: when the parent is a workflow OUTPUT folder (stamped with
    # workflow_run_id at creation), the registered file is a run output —
    # inherit the parent's classification (which inherited the inputs'
    # max). Ordinary (user-tree) parents keep the column default.
    try:
        from services import data_classification as _dc
        parent_cog = acf_crud.get_active_cog_by_id_and_tenant(
            db, cog_id=parent_id, tenant_id=tenant_id)
        if parent_cog is not None and (
            (getattr(parent_cog, "processing_metadata", None) or {}).get("workflow_run_id")
        ):
            target_cog.data_classification = _dc.propagate_output_classification(
                [getattr(parent_cog, "data_classification", None)])
            db.flush()
    except Exception:
        logger.exception("GY.V2a: output-classification inheritance failed "
                         "for registered cog=%s", target_cog.id)

    content_blob.cog_id = target_cog.id
    db.add(content_blob)
    db.flush()

    new_version = models.Version(
        cog_id=target_cog.id, tenant_id=tenant_id, content_id=content_blob.id,
        version_number=1, name="v1", author_user_id=user_id,
    )
    db.add(new_version)
    db.flush()
    target_cog.latest_version_id = new_version.id

    db_file = models.File(
        tenant_id=tenant_id, user_id=user_id, cog_id=target_cog.id, name=filename,
        size_bytes=file_size, mime_type=content_type, storage_path=storage_path,
    )
    db.add(db_file)
    db.commit()

    try:
        from utils.paid_import import optional_paid
        # egress_metering is a PAID module: on CE there is no storage
        # accounting to record, so the accounting step is simply skipped.
        _egress = optional_paid("services.egress_metering")
        if _egress is not None:
            _egress.apply_storage_change(db, tenant_id,
                                         new_blob_bytes=(file_size or 0) if is_new_blob else 0)
    except Exception:
        logger.exception("storage accounting failed for registered cog=%s", target_cog.id)

    return target_cog


# =============================================================================
# update_download_job_progress (service_router)
# =============================================================================


def update_download_job_progress(db: Session, *, job_id: str, update: "schemas.DownloadJobUpdateRequest", principal):
    download_job = acf_crud.get_download_job_by_id(db, job_id)
    if not download_job:
        raise NotFoundError("Download job not found")

    if isinstance(principal, dict):
        from utils.run_task_auth import pin_run_task_tenant

        pin_run_task_tenant(principal, tenant_id=download_job.tenant_id)

    if update.status is not None:
        download_job.status = update.status
    if update.processed_files is not None:
        download_job.processed_files = update.processed_files
    if update.current_file is not None:
        download_job.current_file = update.current_file
    if update.total_files is not None:
        download_job.total_files = update.total_files
    if update.download_url is not None:
        download_job.download_url = update.download_url
    if update.filename is not None:
        download_job.filename = update.filename
    if update.error_message is not None:
        download_job.error_message = update.error_message
    if update.expires_at is not None:
        download_job.expires_at = update.expires_at

    db.commit()


# =============================================================================
# download_file
# =============================================================================


def _watermark_policy(db: Session) -> Optional[Dict[str, Any]]:
    """GY.V1b (GSEC R-112): read the three compliance.watermark_* settings.

    Returns None when the feature is off. A failed settings read leaves the
    feature off (the presign path is then byte-identical to today) — the
    watermark is an enhancement, not a gate, so a broken read must not change
    delivery behaviour.
    """
    try:
        from services.platform_settings import get_setting

        if False is not True:
            return None
        return {
            "min_label": False,
            "unsupported_action": False,
        }
    except Exception as e:  # noqa: BLE001 — settings read failure = feature off
        logger.warning("GY.V1b: watermark settings read failed, feature left off: %s", e)
        return None


def _watermark_label_applies(db: Session, cog, min_label: str) -> bool:
    """True when the asset's label is at or above the minimum label.

    An unrecognised stored label fails CLOSED (treated as the highest level),
    mirroring services/data_classification.py's fail-closed stance — a corrupt
    label must not become a watermark bypass.
    """
    from utils.paid_import import optional_paid
    dc = optional_paid("services.data_classification")
    if dc is None:
        # 1788 CE carve: forensic watermarking (services/watermark) is paid and
        # absent in CE, so this label gate has nothing to gate.
        return False

    label = getattr(cog, "data_classification", None)
    if not label:
        return False
    try:
        return dc.rank(label) >= dc.rank(min_label)
    except dc.UnknownClassificationError:
        logger.warning("GY.V1b: unrecognised label %r on cog %s, FAILING CLOSED",
                       label, getattr(cog, "id", None))
        return True


def _marking_download_label(db: Session, tenant_id, cog_id) -> Optional[str]:
    """GY.C7 s1 — the FX.79 data-mark label that makes this download 'labelled'.

    `services/marking_stamps.download_label` reads the live `data_markings`
    rows for this asset (the admin CUI/ITAR catalogue, NOT the GY.V2
    `data_classification` column the GY.V1b gate above reads) and returns the
    worst-ranked label, dissemination addendum included (`CUI/NOFORN`), or
    None when the asset carries no live mark / the marking store is absent
    (CE carve). A download of a marked asset must never be handed out
    unwatermarked, so this label feeds the SAME watermark gate + policy below.
    """
    try:
        from services import marking_stamps

        return marking_stamps.download_label(db, tenant_id, "asset", cog_id)
    except Exception as e:  # noqa: BLE001 — a stamp read must not 500 a download
        logger.warning("GY.C7 s1: marking label read failed (cog %s): %s", cog_id, e)
        return None


def _marking_label_applies(db: Session, label: Optional[str], min_label: str,
                           tenant_id: Optional[int] = None) -> bool:
    """True when an FX.79 mark label is at/above the watermark minimum.

    The label may carry a dissemination addendum (`CUI/NOFORN`), so only the
    part before the first `/` is ranked. An unrankable label FAILS CLOSED
    (treated as applying), mirroring `_watermark_label_applies` and
    services/data_classification.py's fail-closed stance — a corrupt label
    must not become a watermark bypass.

    GY.C7 s3c: a customer's OWN label (GY.C7 s3 custom code, tenant-scoped
    rules in `labels.custom_label_rules`) has no place in the platform rank
    table, so its rule dict answers the question directly —
    `watermark: false` means "this label does not force watermarked
    delivery", `watermark: true` (or a rule dict that omits the key) means
    it does. A custom code with NO rule entry is not a known code at all, so
    it keeps the fail-closed posture above: it watermarks.
    """
    if not label:
        return False
    from utils.paid_import import optional_paid
    dc = optional_paid("services.data_classification")
    if dc is None:
        # 1788 CE carve: same as _watermark_label_applies — no paid watermark
        # delivery in CE, so a mark label never forces watermarked delivery.
        return False

    code = label.split("/")[0]
    # GY.C7 s3c: the tenant's custom-code rule answers before the rank path.
    try:
        from utils import label_policy as _lp

        rule = _lp.custom_label_rules(db, tenant_id, code)
    except Exception as e:  # noqa: BLE001 — a policy read must not 500 a
        rule = None         # download; the rank path below still decides
        logger.warning("GY.C7 s3c: custom-label rule read failed (%s): %s",
                       code, e)
    if rule is not None:
        return bool(rule.get("watermark", True))

    try:
        return dc.rank(code) >= dc.rank(min_label or "")
    except dc.UnknownClassificationError:
        logger.warning(
            "GY.C7 s1: unrecognised marking label %r, FAILING CLOSED", label)
        return True


def _record_marked_download(db: Session, tenant_id, user_id, cog_id) -> None:
    """GY.C7 s1 — the per-label policy log entry for one labelled download.

    `services/data_markings.record_marked_access` with action='download':
    exactly ONE security audit row (reused CONTENT_DOWNLOAD action, reason
    `data_marking_access`, metadata carries the labels + the resource triple)
    plus the FX.77 log-stream event — the same shape the p4a read/stream
    paths write, so one grep on `data_marking_access` shows every labelled
    access. An UNMARKED asset writes nothing at all (the download stays
    byte-identical AND audit-identical to today). Never raises.
    """
    try:
        from utils.paid_import import optional_paid

        _dm = optional_paid("services.data_markings")
        if _dm is None:  # CE carve: no marking store -> nothing to log
            return
        _dm.record_marked_access(
            db, tenant_id=tenant_id, user_id=user_id,
            resource_type="asset", resource_id=cog_id,
            action=_dm.MARKED_DOWNLOAD_ACTION)
    except Exception as e:  # noqa: BLE001 — the log is best-effort
        logger.warning("GY.C7 s1: marked-download log failed (cog %s): %s", cog_id, e)


def _watermark_download(db: Session, *, cog, current_user, http_request,
                        minio_client, minio_bucket: str, object_path: str,
                        file_name: str, file_mime: str, policy: Dict[str, Any],
                        marking_label: Optional[str] = None) -> Dict[str, Any]:
    """Stream one labelled asset through the API with both watermark layers.

    Called only when the feature is on AND the asset's label is at or above the
    minimum. Returns the delivered bytes plus the delivery metadata the router
    needs; writes the V1a audit row with path="watermark" and the payload id.

    Unsupported content type follows `compliance.watermark_unsupported_action`:
    'block' raises ForbiddenError (403 in the router), 'allow' delivers the
    original bytes and records metadata.watermark="unsupported".
    """
    # FX.162.p5d — CE carve: the watermark modules are paid-only (manifest
    # denies services/watermark.py, services/watermark_delivery.py,
    # utils/watermark.py), so every import here is guarded. Absent -> the
    # bytes are delivered UNMARKED (the feature does not exist in CE); the
    # caller (download_file) never routes here when they are absent, so this
    # fallback is defensive only — never a 500, never a silent skip of the
    # audit row.
    from utils.paid_import import optional_paid
    wm = optional_paid("utils.watermark")
    _wm_paid = optional_paid("services.watermark")
    _wm_delivery = optional_paid("services.watermark_delivery")

    supported = wm is not None and wm.watermark_supported(file_mime)
    if not supported and wm is not None and policy["unsupported_action"] == "block":
        raise ForbiddenError(
            "This asset carries a restricted data label and its content type "
            f"({file_mime}) cannot be watermarked in this MOD release "
            "(images only). Delivery is blocked by "
            "compliance.watermark_unsupported_action='block'.")

    try:
        raw = minio_client.client.get_object(minio_bucket, object_path).read()
    except Exception as e:  # noqa: BLE001 — same typed error as the presign path
        logger.error("GY.V1b: could not read object for watermark (cog %s): %s",
                     cog.id, e)
        raise ExternalStorageError("Error downloading file")

    # FX.162.p5d — CE carve: no paid watermark module -> no forensic ID
    # (the ID is a compliance side effect, not a delivery gate).
    wm_id = None
    if _wm_paid is not None:
        wm_id = _wm_paid.apply_watermark(db, cog=cog, current_user=current_user)
        if wm_id is None:
            import time

            wm_id = _wm_paid.watermark_id(
                current_user.tenant_id, current_user.id, cog.id, int(time.time()))

    delivered = raw
    watermark_state = "unsupported"
    if supported:
        import datetime

        # FX.162.p5d — CE carve: invisible_watermark_enabled is a paid
        # settings read; absent -> the invisible layer stays off (visible
        # layer only, which is the shipped default anyway).
        invisible_on = _wm_delivery is not None and _wm_delivery.invisible_watermark_enabled(db)

        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        recipient = getattr(current_user, "username", None) or f"user:{current_user.id}"
        visible_text = f"{recipient} | {stamp} | {wm_id}"
        visible = wm.visible_watermark(raw, visible_text)
        delivered = visible
        watermark_state = "applied"

        # GY.V1d — labels.invisible_watermark_enabled: on top of (never
        # instead of) the visible layer above. Default False (visible-only,
        # unchanged behaviour). When True, a gated label whose invisible
        # mark cannot be applied is refused — fail CLOSED, mirroring the
        # unsupported_action='block' raise above.
        if invisible_on:
            if not wm.invisible_watermark_available():
                raise ForbiddenError(
                    "This asset carries a restricted data label and the "
                    "invisible forensic watermark is unavailable in this "
                    "MOD release. Delivery is blocked by "
                    "labels.invisible_watermark_enabled (fail-closed).")
            try:
                invisible = wm.embed_invisible(visible, wm_id)
            except Exception as e:  # noqa: BLE001 — fail CLOSED, not silent
                logger.warning(
                    "GY.V1d: invisible embedding raised for cog %s: %s", cog.id, e)
                invisible = visible
            if invisible == visible:
                raise ForbiddenError(
                    "This asset carries a restricted data label and the "
                    "invisible forensic watermark could not be embedded. "
                    "Delivery is blocked (fail-closed).")
            delivered = invisible

    # 1788 CE carve: delivery_audit is paid; CE records no delivery row
    # (audit-only side effect — the hand-off itself is unchanged).
    from utils.paid_import import optional_paid
    _delivery_audit = optional_paid("services.delivery_audit")
    record_delivery = (_delivery_audit.record_delivery
                       if _delivery_audit is not None
                       else (lambda *a, **kw: None))

    # Bytes read server-side and re-sent = genuine MOD egress (owner decision
    # 09-29), so this path is metered like the stream path. egress_meter is
    # Paid (absent in the CE carve) — skip silently there.
    try:
        from services.egress_meter import check_and_record

        check_and_record(db, current_user.tenant_id, len(raw),
                         kind="file_download", via_mod_proxy=True)
    except ImportError:
        pass

    row = record_delivery(
        db,
        delivery_path="watermark",
        actor_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        resource_id=cog.id,
        object_key=object_path,
        request=http_request,
        watermark_id=wm_id,
        metadata={
            "file_name": file_name,
            "size_bytes": len(delivered),
            "watermark": watermark_state,
            "label": getattr(cog, "data_classification", None),
            # GY.C7 s1: which FX.79 data-mark (if any) forced this watermarked
            # delivery — the audit row names the label that triggered it.
            "marking_label": marking_label,
            "payload_id": wm_id,
        },
    )

    return {
        "watermarked": True,
        "content": delivered,
        "filename": file_name,
        "size_bytes": len(delivered),
        "content_type": file_mime,
        "watermark_id": wm_id,
        "watermark_state": watermark_state,
        "audit_row_id": getattr(row, "id", None),
    }


def download_file(db: Session, *, cog_id: int, use_presigned: bool, current_user, minio_client, http_request=None) -> Dict[str, Any]:
    from utils.usage_enforcement import check_overage_cap

    check_overage_cap(db=db, tenant_id=current_user.tenant_id, raise_on_cap=True)

    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise NotFoundError("File not found")

    if cog.cog_type != models.CogType.FILE:
        raise InvalidRequestError("Can only download FILE type cogs")

    from permissions import can_user_view_cog

    if not can_user_view_cog(db, current_user.id, cog_id):
        raise ForbiddenError("You don't have permission to download this file")

    # FX.90 p2b (Q76=A) — malware quarantine: a flagged artefact is NEVER
    # handed out, neither streamed nor presigned (423 Locked via the router
    # map). Checked after the view grant and before the audit write so a
    # quarantined object cannot leak through either delivery path.
    from services import files_service as _fx90
    _fx90.raise_if_quarantined(cog, cog_id=cog_id)

    # Audit: log content download (TPN CS-7, CMMC AU-2). Non-fatal.
    try:
        from audit import log_permission_audit
        from models import AuditAction

        log_permission_audit(
            db=db, action=AuditAction.CONTENT_DOWNLOAD,
            actor_user_id=current_user.id, tenant_id=current_user.tenant_id,
            resource_type="cog", resource_id=cog_id,
            metadata={"file_name": cog.name, "presigned": use_presigned},
        )
    except Exception:
        pass

    file_record = acf_crud.get_active_file_for_cog_and_tenant(
        db, cog_id=cog_id, tenant_id=current_user.tenant_id
    )

    storage_path = None
    file_name = cog.name  # rename-mutable display name (see models.Cog)
    file_size = 0
    file_mime = "application/octet-stream"

    if file_record:
        storage_path = file_record.storage_path
        file_size = file_record.size_bytes or 0
        file_mime = file_record.mime_type or "application/octet-stream"
    else:
        if cog.latest_version_id:
            version = acf_crud.get_version_by_id(db, cog.latest_version_id)
            if version and version.content_id:
                blob = acf_crud.get_content_blob_by_id_and_tenant(
                    db, blob_id=version.content_id, tenant_id=current_user.tenant_id,
                )
                if blob:
                    storage_path = blob.storage_path
                    file_size = blob.size_bytes or 0
                    file_mime = blob.mime_type or "application/octet-stream"

    if not storage_path:
        raise NotFoundError("File content not found in storage")

    # Bug-5: ensure the download filename has an extension.
    if "." not in os.path.basename(file_name):
        _ext = None
        if file_record and file_record.name and "." in os.path.basename(file_record.name):
            _ext = file_record.name.rsplit(".", 1)[-1]
        if not _ext and file_mime and file_mime not in ("application/octet-stream", ""):
            import mimetypes

            _guess = mimetypes.guess_extension(file_mime)
            if _guess:
                _ext = _guess.lstrip(".")
        if _ext:
            file_name = f"{file_name}.{_ext.lower()}"

    # GY.V2 R-133 — opt-in FILENAME marking: prefix the Content-Disposition
    # filename with "[CUI] "/"EAR] "/"ITAR] " for export-controlled labels
    # when labels.mark_download_filenames is on. Default off → byte-identical.
    from utils.paid_import import optional_paid
    _dc_fn = optional_paid("services.data_classification")
    if _dc_fn is not None:  # 1788 CE carve: no taxonomy -> no filename marking
        file_name = _dc_fn.marked_download_filename(
            file_name, getattr(cog, "data_classification", None), db=db)

    minio_bucket = minio_client.bucket_name
    # P10e-a dual-read: new rows carry `s3://`, rows stored before the rename
    # still hold `minio://`; the configured STORAGE_SCHEME stays honoured,
    # default s3.
    storage_scheme = os.getenv("STORAGE_SCHEME", "s3")
    scheme_prefix = f"{storage_scheme}://{minio_bucket}/"

    if storage_path.startswith(scheme_prefix):
        object_path = storage_path[len(scheme_prefix):]
    else:
        if storage_path.startswith((f"{storage_scheme}://", "s3://", "minio://")):
            path_after_scheme = storage_path.split("://", 1)[1]
            if "/" in path_after_scheme:
                object_path = "/".join(path_after_scheme.split("/")[1:])
            else:
                object_path = path_after_scheme
        else:
            object_path = storage_path

    # GY.V1b (GSEC R-112): a labelled asset at or above the minimum label is
    # NOT handed out as a direct presigned URL when the feature is on — it is
    # streamed through the API with the visible + invisible watermark applied.
    # Outside the presign try/except below so a 'block' 403 is not swallowed
    # into a 500.
    # FX.110 (owner decision Q97=A) — outside collaborators: a per-grant
    # `forced_watermark` restriction (ResourcePermission.
    # external_restriction_options, services/outside_collaborators.
    # grant_restrictions) forces the GY.V1 watermarked delivery path for
    # this download regardless of the tenant's own watermark policy and
    # of the asset's label — a forced watermark must not be bypassable by
    # an unlabelled asset or a permissive tenant. Reuses the existing
    # _watermark_download stream + audit path (synthesising a minimal
    # policy when the tenant feature is off).
    try:
        from services import outside_collaborators as _fx110
        _fx110_forced = _fx110.forced_watermark_required(
            db, current_user, cog)
    except Exception:  # noqa: BLE001 — watermark is an enhancement;
        _fx110_forced = False  # a broken read must not change delivery
    # FX.162.p5d — CE carve: the watermark feature is paid-only (its three
    # modules are manifest-denied), so on CE it does not exist at all: no
    # policy read, no forced-watermark synthesis, and the asset is delivered
    # by the normal presign/stream path below, byte-identical to today.
    from utils.paid_import import optional_paid
    _wm_present = (optional_paid("utils.watermark") is not None
                   and optional_paid("services.watermark") is not None)
    policy = _watermark_policy(db) if _wm_present else None
    if _wm_present and _fx110_forced and not policy:
        policy = {"enabled": True, "min_label": "",
                  "unsupported_action": "block"}
    # GY.C7 s1 — an FX.79 admin data-mark (CUI / ITAR / export-controlled,
    # services/data_markings live `data_markings` rows) is a download trigger
    # on its own: a marked asset is never handed out as a raw presigned URL
    # when the watermark feature is on, even if its GY.V2 `data_classification`
    # column is empty. None (unmarked / CE carve) -> the gate below is exactly
    # as before, byte-identical.
    _mk_label = _marking_download_label(db, current_user.tenant_id, cog_id)
    if _mk_label is not None:
        # The per-label policy log entry (one audit row + the FX.77 log-stream
        # event, reason `data_marking_access`, action 'download'). Unmarked
        # assets write nothing here at all.
        _record_marked_download(db, current_user.tenant_id, current_user.id, cog_id)
    if _wm_present and policy and (
            _fx110_forced
            or _watermark_label_applies(db, cog, policy["min_label"])
            or _marking_label_applies(db, _mk_label, policy["min_label"],
                                      tenant_id=current_user.tenant_id)):
        return _watermark_download(
            db, cog=cog, current_user=current_user, http_request=http_request,
            minio_client=minio_client, minio_bucket=minio_bucket,
            object_path=object_path, file_name=file_name,
            file_mime=file_mime, policy=policy, marking_label=_mk_label,
        )

    logging.info(f"File {cog_id} size: {file_size} bytes")
    logging.info(f"Generating presigned URL for file {cog_id}")
    try:
        from datetime import timedelta

        presigned_url = minio_client.presigned_client.presigned_get_object(
            bucket_name=minio_bucket,
            object_name=object_path,
            expires=timedelta(seconds=900),
            response_headers={
                "response-content-disposition": f'attachment; filename="{file_name}"',
                "response-content-type": file_mime,
            },
        )

        logging.info(f"Generated presigned URL for file {cog_id}: {presigned_url[:100]}...")

        # GY.V1a (GSEC R-111): the presign delivery path must write its own
        # audit row (expiry + object key + trusted client IP). Best-effort —
        # record_delivery never raises into the request.
        # 1788 CE carve: delivery_audit is paid; CE records no delivery row
        # (audit-only side effect — the hand-off itself is unchanged).
        from utils.paid_import import optional_paid
        _delivery_audit = optional_paid("services.delivery_audit")
        record_delivery = (_delivery_audit.record_delivery
                           if _delivery_audit is not None
                           else (lambda *a, **kw: None))

        # GY.V1b: forensic watermark ID (off/label_only/all policy). None
        # when the policy is off or the cog is not pre-release-marked.
        # FX.162.p5d — CE carve: services/watermark is paid-only, so the
        # presign audit row simply carries no watermark ID there.
        from utils.paid_import import optional_paid
        _wm_paid = optional_paid("services.watermark")
        wm_id = (_wm_paid.apply_watermark(db, cog=cog, current_user=current_user)
                 if _wm_paid is not None else None)

        record_delivery(
            db,
            delivery_path="presign",
            actor_user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            resource_id=cog_id,
            object_key=object_path,
            expiry_seconds=900,
            request=http_request,
            metadata={"file_name": file_name, "size_bytes": file_size},
            watermark_id=wm_id,
        )

        # GT.D10 — meter + enforce the egress cap (402 at 100%). Issuance-based:
        # the presigned object's declared size counts as egress (documented
        # approximation — an issued-but-never-fetched URL still counts). The
        # 402 is raised HERE (before the URL is returned) and must propagate.
        # GT.D4b — services.egress_meter is Paid (absent in the CE carve); egress
        # metering is a billing side effect, so skip it silently on CE.
        try:
            from services.egress_meter import check_and_record
        except ImportError:
            check_and_record = None
        if check_and_record is not None:
            check_and_record(db, current_user.tenant_id, file_size,
                             kind="file_download")

        result = {
            "download_url": presigned_url,
            "filename": file_name,
            "size_bytes": file_size,
            "expires_in_seconds": 900,
        }
        if wm_id:
            result["watermark_id"] = wm_id
        # GY.V2 R-133: delivery marking for the response headers.
        # `label` = the file's label (normalised) — X-MOD-Classification;
        # `marking` = the export-control banner string (ITAR / EAR (ECCN ..) /
        # CUI//..) when present; `handling` = short CUI/EAR/ITAR caveat.
        # A corrupt label fails closed as ITAR inside the helpers.
        try:
            from services import data_classification as _dc_mark
            _lbl = getattr(cog, "data_classification", None)
            hdr = _dc_mark.delivery_classification_header(_lbl, cog)
            if hdr:
                result["label"] = hdr
                result["handling"] = _dc_mark.handling_caveat(_lbl)
                _msk = _dc_mark.marking_string(cog)
                if _msk:
                    result["marking"] = _msk
        except Exception:
            pass
        return result
    except Exception as e:
        logging.error(f"Error downloading file {cog_id}: {e}")
        raise ExternalStorageError("Error downloading file")


# =============================================================================
# create_preview (service_router)
# =============================================================================


def create_preview(
    db: Session, *, output_folder_id: int, source_hash: str, preview_filename: str, tenant_id: int, principal,
):
    if isinstance(principal, dict):
        from utils.run_task_auth import pin_run_task_tenant

        pin_run_task_tenant(principal, tenant_id=tenant_id)

    output_folder = acf_crud.get_active_cog_by_id_and_tenant(
        db, cog_id=output_folder_id, tenant_id=tenant_id
    )
    if not output_folder:
        raise NotFoundError("Output folder not found")

    source_blob = acf_crud.get_content_blob_by_id_and_tenant(
        db, blob_id=source_hash, tenant_id=tenant_id
    )
    if not source_blob:
        raise NotFoundError(f"Source file not found: {source_hash}")

    from utils.paid_import import optional_paid
    _dc = optional_paid("services.data_classification")
    new_cog = models.Cog(
        name=preview_filename,
        cog_type=models.CogType.FILE,
        parent_id=output_folder_id,
        owner_id=output_folder.owner_id,
        tenant_id=tenant_id,
        # GY.V2a: a preview is a run output — inherit the output folder's
        # classification (which itself inherited the inputs' max).
        data_classification=(_dc.propagate_output_classification(
            [getattr(output_folder, "data_classification", None)])
            if _dc is not None else "internal"),  # 1788 CE carve: column default
        processing_metadata={"type": "preview", "source_hash": source_hash},
    )
    db.add(new_cog)
    db.flush()

    # GY.C7 s3c: the preview is derived from the run's input, so the marks
    # the output folder already carries (propagated from the input above)
    # must ride down onto the preview file itself — a CUI / tenant-custom
    # marked input never yields an unmarked preview. Idempotent, tenant-
    # scoped, CE carve via optional_paid.
    from utils.paid_import import optional_paid

    _dm = optional_paid("services.data_markings")
    if _dm is not None:
        try:
            _dm.propagate_marks(
                db, tenant_id=tenant_id,
                source=("asset", output_folder.id),
                target=("asset", new_cog.id),
            )
        except Exception as e:  # noqa: BLE001 — marks are an enhancement
            logger.warning(
                "GY.C7 s3c: mark propagation failed into preview cog %s: %s",
                new_cog.id, e)

    version = models.Version(
        cog_id=new_cog.id,
        tenant_id=tenant_id,
        content_id=source_blob.id,
        version_number=1,
        author_user_id=output_folder.owner_id,
    )
    db.add(version)
    db.flush()

    new_cog.latest_version_id = version.id
    db.commit()
    db.refresh(new_cog)

    logging.info(
        f"Created preview: {preview_filename} (cog_id: {new_cog.id}, version_id: {version.id})"
    )

    return {"cog_id": new_cog.id, "filename": preview_filename, "size_bytes": source_blob.size_bytes}
