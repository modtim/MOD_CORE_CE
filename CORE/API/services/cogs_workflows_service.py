"""Service layer for `routers/cogs/workflows.py` (openspec:
stack-training-and-prewarm, Gate 2b / D9).

Business logic for the three handlers migrated out of that router. All data
access goes through `crud/cogs_workflows.py`; HTTP concerns (`HTTPException`,
docstrings, `response_model`) stay in the router. This module raises
`services._base.ServiceError` subclasses where a router-visible error is
needed, and returns plain values (dict/str) otherwise — behaviour, log lines
and response shapes are unchanged from the pre-migration inline versions.

Heavy/app modules (`models`) are imported lazily inside functions, matching
the function-local-import convention used throughout this codebase.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from crud import cogs_workflows as cw_crud
from services._base import NotFoundError, ServiceError

logger = logging.getLogger(__name__)


# =============================================================================
# get_multiple_cogs_files — zip_folder_download service endpoint
# =============================================================================


def get_multiple_cogs_files(
    db: Session, *, cog_ids: List[int], principal, start_time: float = None
) -> Dict[str, Any]:
    """Get all files from multiple cogs (files and/or folders), recursively
    expanding folders. Unchanged algorithm/log lines from the pre-migration
    inline version — only the `db.query(...)` calls moved to crud.

    `start_time` is passed in by the router, which logs the "REQUEST START"
    line and does the empty-`cog_ids` validation BEFORE calling here (matching
    the pre-migration handler's exact statement order); falls back to `now()`
    for direct/test callers that don't pass it.
    """
    import models
    from utils.run_task_auth import pin_run_task_tenant

    if start_time is None:
        start_time = time.time()

    # Cross-tenant guard: a run_task node token may only read cogs in its own tenant.
    if isinstance(principal, dict):
        for _c in cw_crud.get_cogs_by_ids(db, cog_ids):
            pin_run_task_tenant(principal, tenant_id=_c.tenant_id)

    def get_files_recursive(cog_id: int, current_path: str = ""):
        """Recursively collect all files in folder tree"""
        files_list = []
        # Get all active (non-deleted) children
        children = cw_crud.get_active_children(db, cog_id)

        for child in children:
            if child.cog_type == models.CogType.FILE:
                if child.latest_version_id:
                    current_version = cw_crud.get_version_by_id(db, child.latest_version_id)
                else:
                    current_version = None

                if current_version:
                    content_blob = cw_crud.get_content_blob_for_version(
                        db, tenant_id=child.tenant_id, content_id=current_version.content_id
                    )

                    if content_blob:
                        storage_path_str = content_blob.storage_path
                        if storage_path_str.startswith(("s3://", "minio://")):
                            storage_path_str = storage_path_str.split("://", 1)[1]
                            if "/" in storage_path_str:
                                storage_path_str = storage_path_str.split("/", 1)[1]

                        relative_path = f"{current_path}{child.name}" if current_path else child.name
                        files_list.append({
                            "cog_id": child.id,
                            "name": child.name,
                            "minio_path": storage_path_str,
                            "relative_path": relative_path,
                            "file_size": content_blob.size_bytes,
                            "mime_type": content_blob.mime_type,
                            "file_extension": content_blob.file_extension or ""
                        })

            elif child.cog_type in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
                subfolder_path = f"{current_path}{child.name}/" if current_path else f"{child.name}/"
                files_list.extend(get_files_recursive(child.id, subfolder_path))

        return files_list

    def get_single_file_info(cog):
        """Get file info for a single file cog"""
        if cog.latest_version_id:
            current_version = cw_crud.get_version_by_id(db, cog.latest_version_id)
        else:
            return None

        if not current_version:
            return None

        content_blob = cw_crud.get_content_blob_for_version(
            db, tenant_id=cog.tenant_id, content_id=current_version.content_id
        )

        if not content_blob:
            return None

        storage_path_str = content_blob.storage_path
        if storage_path_str.startswith(("s3://", "minio://")):
            storage_path_str = storage_path_str.split("://", 1)[1]
            if "/" in storage_path_str:
                storage_path_str = storage_path_str.split("/", 1)[1]

        return {
            "cog_id": cog.id,
            "name": cog.name,
            "minio_path": storage_path_str,
            "relative_path": cog.name,
            "file_size": content_blob.size_bytes,
            "mime_type": content_blob.mime_type,
            "file_extension": content_blob.file_extension or ""
        }

    all_files = []
    cog_names = []

    # Check if this is a single folder download (contents only, no nesting)
    is_single_folder = len(cog_ids) == 1

    for cog_id in cog_ids:
        # Exclude soft-deleted cogs from download
        cog = cw_crud.get_active_cog(db, cog_id)
        if not cog:
            logging.warning(f"[ZIP-MULTI] Cog {cog_id} not found or deleted, skipping")
            continue

        cog_names.append(cog.name)

        if cog.cog_type == models.CogType.FILE:
            # Single file - add directly
            file_info = get_single_file_info(cog)
            if file_info:
                all_files.append(file_info)
        elif cog.cog_type in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
            # For single folder: get contents without folder prefix (flat structure)
            # For multiple items: include folder name as prefix to preserve structure
            if is_single_folder:
                folder_files = get_files_recursive(cog.id, "")
            else:
                folder_files = get_files_recursive(cog.id, f"{cog.name}/")
            all_files.extend(folder_files)

    # Generate a combined name for the zip
    if len(cog_names) == 1:
        combined_name = cog_names[0]
    elif len(cog_names) <= 3:
        combined_name = "_".join(cog_names)
    else:
        combined_name = f"{len(cog_names)}_items"

    response_data = {
        "cog_ids": cog_ids,
        "folder_name": combined_name,
        "files": all_files,
        "total_files": len(all_files),
        "total_size": sum(f["file_size"] for f in all_files)
    }

    end_time = time.time()
    duration = end_time - start_time
    logging.info(f"[ZIP-MULTI] === REQUEST END === Took {duration:.3f}s, returning {len(all_files)} files")
    return response_data


# =============================================================================
# preview-url
# =============================================================================


def get_preview_url(db: Session, *, cog_id: int, current_user, minio_client) -> str:
    """Resolve a presigned preview URL for a cog. Raises `NotFoundError` when
    the cog or its file content is missing, `ServiceError` on a presign
    failure — the router maps both to the pre-migration HTTP status/detail."""
    from datetime import timedelta

    import crud

    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise NotFoundError("File not found")

    file_record = cw_crud.get_active_file_for_cog(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not file_record:
        raise NotFoundError("File content not found")

    # Extract object path from storage_path (P10e-a dual-read: new rows carry
    # `s3://`, rows stored before the rename still hold `minio://`; the
    # configured STORAGE_SCHEME stays honoured, default s3).
    storage_scheme = os.getenv('STORAGE_SCHEME', 's3')
    sp = file_record.storage_path or ''
    if sp.startswith((f'{storage_scheme}://', 's3://', 'minio://')):
        path_after_scheme = sp.split('://', 1)[1]
        object_path = '/'.join(path_after_scheme.split('/')[1:]) if '/' in path_after_scheme else path_after_scheme
    else:
        object_path = sp

    try:
        url = minio_client.presigned_client.presigned_get_object(
            bucket_name=minio_client.bucket_name,
            object_name=object_path,
            expires=timedelta(seconds=900),
        )
    except Exception as e:
        logger.error("Error generating preview URL: %s", e)
        raise ServiceError("Error generating preview URL") from e

    # GY.V1a follow-up (GSEC R-111): GET /cogs/{id}/preview-url hands the user
    # a presigned inline-preview GET, so it gets its own delivery row. IDs
    # only — cog id + the object key; the URL itself is never stored.
    # 1788 CE carve: delivery_audit is paid; CE records no delivery row
    # (audit-only side effect — the hand-off itself is unchanged).
    from utils.paid_import import optional_paid
    _delivery_audit = optional_paid("services.delivery_audit")
    record_delivery = (_delivery_audit.record_delivery
                       if _delivery_audit is not None
                       else (lambda *a, **kw: None))

    record_delivery(
        db,
        delivery_path="presign",
        actor_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        resource_id=cog_id,
        object_key=object_path,
        expiry_seconds=900,
        metadata={"channel": "workflow_cog", "asset": "preview"},
        # Poll-hit site: one row per presign window (per process).
        dedupe_window_s=900,
    )
    return url


# =============================================================================
# text-content
# =============================================================================


def get_text_content(db: Session, *, cog_id: int, current_user, minio_client) -> str:
    """Read (and, past a size cap, truncate) a file's text content for the
    workflow builder / dashboard preview. Raises `NotFoundError` /
    `ServiceError` exactly where the pre-migration inline version raised
    404 / 500."""
    import crud

    cog = crud.get_cog_by_id_and_tenant(db, cog_id=cog_id, tenant_id=current_user.tenant_id)
    if not cog:
        raise NotFoundError("File not found")

    # Get storage path from File record or ContentBlob fallback
    storage_path = None
    file_size = 0

    file_record = cw_crud.get_active_file_for_cog(db, cog_id=cog_id, tenant_id=current_user.tenant_id)

    if file_record:
        storage_path = file_record.storage_path
        file_size = file_record.size_bytes or 0
    elif cog.latest_version_id:
        version = cw_crud.get_version_by_id(db, cog.latest_version_id)
        if version and version.content_id:
            blob = cw_crud.get_content_blob_for_version(
                db, tenant_id=current_user.tenant_id, content_id=version.content_id
            )
            if blob:
                storage_path = blob.storage_path
                file_size = blob.size_bytes or 0

    if not storage_path:
        raise NotFoundError("File content not found")

    # Cap the amount of text streamed into the browser preview. Rather than
    # refusing large files with a 413 (a CSV a few thousand rows long trivially
    # exceeds the old 500KB limit, and the user only saw a raw "HTTP 413"),
    # preview the FIRST slice of any file and mark it truncated — the full file
    # is always available via download.
    preview_cap = int(os.getenv("TEXT_PREVIEW_MAX_BYTES", str(5 * 1024 * 1024)))  # 5MB
    truncated = file_size > preview_cap

    # Extract object path (P10e-a dual-read: new rows carry `s3://`, rows
    # stored before the rename still hold `minio://`; the configured
    # STORAGE_SCHEME stays honoured, default s3).
    storage_scheme = os.getenv('STORAGE_SCHEME', 's3')
    sp = storage_path
    if sp.startswith((f'{storage_scheme}://', 's3://', 'minio://')):
        path_after_scheme = sp.split('://', 1)[1]
        object_path = '/'.join(path_after_scheme.split('/')[1:]) if '/' in path_after_scheme else path_after_scheme
    else:
        object_path = sp

    try:
        if truncated:
            response = minio_client.client.get_object(
                minio_client.bucket_name, object_path, offset=0, length=preview_cap)
        else:
            response = minio_client.client.get_object(minio_client.bucket_name, object_path)
        content = response.read().decode('utf-8', errors='replace')
        response.close()
        response.release_conn()
        if truncated:
            content += (
                f"\n\n--- preview truncated: showing the first "
                f"{preview_cap / (1024 * 1024):.0f} MB of {file_size / (1024 * 1024):.1f} MB. "
                f"Download the file to view all of it. ---\n"
            )
        return content
    except Exception as e:
        logger.error("Error reading file: %s", e)
        raise ServiceError("Error reading file") from e
