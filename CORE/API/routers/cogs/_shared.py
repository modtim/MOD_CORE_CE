# CORE/routers/cogs.py

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, status, HTTPException, UploadFile, File, Form, Request, Header
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import func, Float, or_
from sqlalchemy.exc import IntegrityError
import uuid  # For generating unique object names in MinIO
import hashlib
import logging
import os

# Setup logging
logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data)
from services.debug_mode import is_debug_mode

# Assuming these are correctly imported from your project structure
import schemas, models, crud
from dependencies import get_db
from utils.minio_client import MinioClient, get_tenant_bucket_name
from models import CogType, ContentBlob, Version  # Explicitly import models needed for direct instantiation
from dependencies import require_active_and_onboarded_user, require_service_account_token, require_bearer_or_api_key
from utils.run_task_auth import node_or_service_principal, pin_run_task_tenant
from utils.registries import platform_image

from dependencies import get_tenant_minio_client

from permissions import (
    can_user_view_cog,
    can_user_upload_to_cog,
    can_user_create_subfolder,
    can_user_move_files_in_cog,
    can_user_delete_cog
)
import audit
from services import cogs_shared_service


def _maybe_audit_xtenant_block(
    db: Session,
    current_user: models.User,
    cog_id: Optional[int],
    request: Optional[Request] = None,
) -> None:
    """data-files-launch-bulletproof §0 — when a platform admin gets a 403
    from a cogs.py route, check whether the cog they tried to access lives
    in another tenant. If yes, emit a debounced audit event for the ISO
    27001 / SOC 2 data-isolation contract. This is best-effort and never
    raises (audit failures must not block the 403 response).

    Caller wires this in immediately BEFORE every `raise HTTPException(403)`
    that fires from a permission check on a per-cog action."""
    return cogs_shared_service.maybe_audit_xtenant_block(db, current_user, cog_id, request)



# Service router for service-to-service authenticated calls (no user database lookup required)

# Public router for endpoints that authenticate via a ?token= query
# parameter instead of an Authorization header. Browser iframes (the
# Dashboard's PDF preview) cannot attach custom headers to the GET that
# the <iframe src="..."> issues, so they pass the access token in the
# URL and the endpoint validates it inline. Routes registered on the
# main `router` would 401 before reaching that inline check because the
# router-level `Depends(require_active_and_onboarded_user)` fires first
# and looks at the missing Authorization header.


def _get_or_create_tenant_root_folder(db: Session, user: models.User) -> models.Cog:
    """Return the tenant's root MASTER_FOLDER, creating it on first use.

    A tenant that's never created a folder still needs somewhere to drop
    uploads. Creating one lazily here removes the "No root folder found"
    upload-blocking error and matches the natural mental model: a fresh
    user can drag-drop a file into a fresh tenant and it just works.

    The auto-created folder is named "My Files" and owned by the
    requesting user; subsequent users in the tenant share it.
    """
    return cogs_shared_service.get_or_create_tenant_root_folder(db, user)


def _attach_original_paths(db: Session, cogs, tenant_id: int) -> None:
    """Attach a human-readable ``original_path`` to each cog in ``cogs``.

    Resolves the parent-folder chain (root → ... → immediate parent) into a
    path like ``/My Files/Reports`` and sets it on ``cog.original_path``.
    Used by the Deleted view so a soft-deleted item shows where it lived
    before deletion. Ancestors may themselves be soft-deleted, so lookups
    intentionally do NOT filter on ``deleted_at``.

    Batched: gathers all ancestor ids level-by-level and resolves them with a
    handful of queries rather than one walk per row.
    """
    return cogs_shared_service.attach_original_paths(db, cogs, tenant_id)


def _find_name_collision(
    db: Session,
    *,
    name: str,
    parent_id: Optional[int],
    tenant_id: int,
    exclude_cog_id: Optional[int] = None,
) -> Optional[models.Cog]:
    """Look up a non-deleted sibling with the same name in the
    destination folder. Returns the colliding row or None.

    `exclude_cog_id` lets a rename-in-place skip itself (a no-op rename
    on a cog should not collide with the cog itself)."""
    return cogs_shared_service.find_name_collision(
        db,
        name=name,
        parent_id=parent_id,
        tenant_id=tenant_id,
        exclude_cog_id=exclude_cog_id,
    )


def _name_collision_409(
    *,
    code: str,
    existing: models.Cog,
    target_parent_id: Optional[int],
    incoming_name: str,
):
    """Build the structured 409 the dashboard's collision modal
    consumes. Always uses the same shape regardless of the operation
    (copy / move / rename) so the client has one code path."""
    target = {"id": target_parent_id, "name": None}
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": code,
            "message": (
                f'An item named "{incoming_name}" already exists in the '
                f'destination. Rename, replace, or cancel.'
            ),
            "existing": {
                "id": existing.id,
                "name": existing.name,
                "cog_type": (
                    existing.cog_type.value
                    if hasattr(existing.cog_type, "value")
                    else str(existing.cog_type)
                ),
                "modified_at": (
                    existing.updated_at.isoformat()
                    if getattr(existing, "updated_at", None) else None
                ),
                "size_bytes": None,  # Filled by client from File record if needed
            },
            "target_folder": target,
            "incoming_name": incoming_name,
        },
    )


def _snapshot_cog_permissions(db: Session, cog_id: int) -> list:
    """Capture the ACTIVE (non-deleted) grants on a cog so a replace can
    re-apply them to the replacement cog. Without this, replacing a file
    (delete old cog + create new) orphans every share — both/all users who had
    access silently lose it (crud.delete_cog soft-deletes the cog's permissions
    too). Returns a list of plain dicts (safe to carry across a delete)."""
    from cog_grants import list_cog_grants  # Gate 30: reconstructed sharing grants
    rows = list_cog_grants(db, cog_ids=[cog_id])
    snap = []
    for p in rows:
        snap.append({
            "level": p.level.value if hasattr(p.level, "value") else str(p.level),
            "user_id": p.user_id,
            "team_id": p.team_id,
            "keycloak_group_id": p.keycloak_group_id,
            "granted_by_user_id": p.granted_by_user_id,
            "expires_at": p.expires_at.isoformat() if p.expires_at else None,
            "is_external_user": p.is_external_user,
            "can_upload": p.can_upload,
            "can_create_subfolders": p.can_create_subfolders,
            "can_move_files": p.can_move_files,
            "can_process": p.can_process,
            "can_share": p.can_share,
            "can_delete": p.can_delete,
        })
    return snap


def _apply_permission_snapshot(db: Session, cog_id: int, snapshot: list) -> None:
    """Re-create the grants captured by _snapshot_cog_permissions on a new cog
    (used after a replace). Best-effort per row; never blocks the upload."""
    if not snapshot:
        return
    from permissions import create_permission as _create_permission
    from models import PermissionLevel as _PermissionLevel
    from datetime import datetime as _dt
    for s in snapshot:
        caps = {k: s[k] for k in (
            "can_upload", "can_create_subfolders", "can_move_files",
            "can_process", "can_share", "can_delete") if s.get(k) is not None}
        try:
            _create_permission(
                db, cog_id=cog_id, level=_PermissionLevel(s["level"]),
                granted_by_user_id=s.get("granted_by_user_id") or s.get("user_id") or 0,
                user_id=s.get("user_id"), team_id=s.get("team_id"),
                keycloak_group_id=s.get("keycloak_group_id"),
                expires_at=_dt.fromisoformat(s["expires_at"]) if s.get("expires_at") else None,
                is_external_user=bool(s.get("is_external_user")),
                **caps,
            )
        except Exception:
            pass
    try:
        db.commit()
    except Exception:
        db.rollback()


def _next_copy_suffix_name(
    db: Session,
    *,
    base_name: str,
    parent_id: Optional[int],
    tenant_id: int,
) -> str:
    """Return a unique sibling name in the destination folder using a
    deterministic " (copy)", " (copy 2)", " (copy 3)", … suffix. Used
    only when the caller has actually hit a name collision (typically
    copy-into-same-folder) — never pre-emptively."""
    candidate = f"{base_name} (copy)"
    if not _find_name_collision(
        db, name=candidate, parent_id=parent_id, tenant_id=tenant_id
    ):
        return candidate
    n = 2
    while n < 1000:
        candidate = f"{base_name} (copy {n})"
        if not _find_name_collision(
            db, name=candidate, parent_id=parent_id, tenant_id=tenant_id
        ):
            return candidate
        n += 1
    # Fallback if truly nothing free in the first 1000 — extremely
    # unlikely; better than infinite loop.
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Too many copies in this folder; rename existing copies and retry.",
    )


def build_folder_path(db: Session, cog_name: str, parent_id: Optional[int], tenant_id: int) -> str:
    """
    Builds the full MinIO folder path by traversing up the parent hierarchy.
    Uses tenant-scoped lookup to support shared folders.
    Returns path like: tenant_123/parent_folder/child_folder/
    """
    path_parts = [cog_name]

    # Traverse up the parent chain using tenant-scoped lookup
    current_parent_id = parent_id
    while current_parent_id:
        parent = crud.get_cog_by_id_and_tenant(db, cog_id=current_parent_id, tenant_id=tenant_id)
        if parent:
            path_parts.insert(0, parent.name)
            current_parent_id = parent.parent_id
        else:
            break

    # Prepend tenant ID for isolation
    path_parts.insert(0, f"tenant_{tenant_id}")

    # Join with / and ensure trailing slash
    return "/".join(path_parts) + "/"
















from pydantic import BaseModel as _BaseModel

class BulkDeleteRequest(_BaseModel):
    cog_ids: List[int]








def _copy_file(
        db: Session,
        source_cog: models.Cog,
        dest_parent_id: Optional[int],
        new_name: str,
        owner_id: int,
        tenant_id: int,
        minio_client: MinioClient
) -> models.Cog:
    """Helper function to copy a file cog and its content in MinIO."""
    return cogs_shared_service.copy_file(
        db, source_cog, dest_parent_id, new_name, owner_id, tenant_id, minio_client
    )


def _copy_folder_recursive(
        db: Session,
        source_cog: models.Cog,
        dest_parent_id: Optional[int],
        new_name: str,
        owner_id: int,
        tenant_id: int,
        minio_client: MinioClient
) -> models.Cog:
    """Helper function to recursively copy a folder and all its contents."""
    return cogs_shared_service.copy_folder_recursive(
        db, source_cog, dest_parent_id, new_name, owner_id, tenant_id, minio_client
    )


# --- Unified File Upload and Version Management ---


# =============================================================================
# --- MULTIPART UPLOAD (Direct S3, supports TB-scale files with resume) ---
# =============================================================================





# =============================================================================
# --- SIMPLE UPLOAD (presigned PUT for smaller files) ---
# =============================================================================













# --- Tag Management ---













# --- PHASE 1: Processing Metadata Endpoints ---











def _processing_image(name: str) -> str:
    """Registry-qualified ref for a cog-processing container (`<name>:latest`).

    2400LR-pd1f: the namespace comes from `utils.registries` (DOCKER_REGISTRY /
    HARBOR_PROJECT, CE_IMAGE_REGISTRY fallback), never a hard-coded project.
    """
    return platform_image(f"{name}:latest")


def _dispatch_thumbnail_task(db, cog_id, content_hash, file_extension, tenant_id, user_id):
    """Dispatch a worker task to generate a 3D thumbnail for a file."""
    from models import ApiTaskQueue, ApiTaskStatus

    service_api_key = os.getenv("PLATFORM_SERVICE_API_KEY", "")
    bucket = get_tenant_bucket_name(tenant_id, db)

    task = ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"thumbnail-{cog_id}",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=5,  # Lower priority than real work
        task_payload={
            "node_kind": "3d_thumbnail_gen",
            "processing_image": _processing_image("3d-thumbnail-gen"),
            "source_file_location": "",
            "minio_bucket": bucket,
            "gpu_required": False,
            "container_timeout": 120,
            "container_env": {
                "CONTENT_HASH": content_hash,
                "FILE_EXTENSION": file_extension.lower().lstrip('.'),
                "COG_ID": str(cog_id),
                "TENANT_ID": str(tenant_id),
                "MINIO_BUCKET": bucket,
                "API_URL": os.getenv("API_BASE_URL", "http://api:80"),
                "API_KEY": service_api_key,
            },
        },
        status=ApiTaskStatus.PENDING,
        max_attempts=1,
        output_namespace="thumbnail",
    )
    db.add(task)
    db.commit()

    # Notify workers
    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:
        pass

    logging.info(f"Dispatched 3D thumbnail task for cog {cog_id} ({content_hash[:12]}...)")


def _dispatch_pointcloud_preview_task(db, cog_id, content_hash, file_extension, tenant_id, user_id):
    """Dispatch a worker task that converts a point cloud (pcd/las/laz/
    xyz/e57) to binary PLY plus a PNG thumbnail. The FE preview path
    pulls the converted PLY through the existing Three.js PLYLoader so
    every point cloud format renders in the unified 3D asset viewer
    (#7 / launch-day-followup-2 §5)."""
    return cogs_shared_service.dispatch_pointcloud_preview_task(
        db, cog_id, content_hash, file_extension, tenant_id, user_id
    )


def _dispatch_sldprt_conversion_task(db, cog_id, content_hash, tenant_id, user_id):
    """Dispatch a worker task that converts a .sldprt to STEP via FreeCAD.
    The result lives in MinIO at `<content_hash>.step` and gets surfaced
    by `GET /cogs/{id}/converted-step` for the browser CAD preview.
    Mirrors `_dispatch_thumbnail_task`'s lifecycle (CPU queue, low
    priority, single-attempt). #8 / launch-day-followup-2 §5.8.
    """
    return cogs_shared_service.dispatch_sldprt_conversion_task(
        db, cog_id, content_hash, tenant_id, user_id
    )


def _dispatch_glb_conversion_task(db, cog_id, content_hash, file_extension, tenant_id, user_id):
    """Dispatch USD→GLB conversion for inline 3D preview. The GLB is rendered via Three.js."""
    from models import ApiTaskQueue, ApiTaskStatus

    service_api_key = os.getenv("PLATFORM_SERVICE_API_KEY", "")
    bucket = get_tenant_bucket_name(tenant_id, db)

    task = ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"glb-convert-{cog_id}",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=5,
        task_payload={
            "node_kind": "usd_glb_convert",
            "processing_image": _processing_image("usd-converter"),
            "source_file_location": content_hash,
            "file_extension": file_extension.lower().lstrip('.'),
            "minio_bucket": bucket,
            "container_env": {
                "TARGET_FORMAT": "glb",
                "COG_ID": str(cog_id),
                "TENANT_ID": str(tenant_id),
                "CONTENT_HASH": content_hash,
                "FILE_EXTENSION": file_extension.lower().lstrip('.'),
                "MINIO_BUCKET": bucket,
                "API_URL": os.getenv("API_BASE_URL", "http://api:80"),
                "API_KEY": service_api_key,
                "PREVIEW_TYPE": "glb",
            },
        },
        status=ApiTaskStatus.PENDING,
        max_attempts=1,
        output_namespace="glb_preview",
    )
    db.add(task)
    db.commit()

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:
        pass

    logging.info(f"Dispatched GLB conversion for cog {cog_id} ({content_hash[:12]}...)")


def _dispatch_hdr_thumbnail_task(db, cog_id, content_hash, file_extension, tenant_id, user_id):
    """Dispatch a worker task to tone-map an HDR/EXR file to a PNG thumbnail."""
    from models import ApiTaskQueue, ApiTaskStatus

    service_api_key = os.getenv("PLATFORM_SERVICE_API_KEY", "")
    bucket = get_tenant_bucket_name(tenant_id, db)

    task = ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"hdr-thumb-{cog_id}",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=5,
        task_payload={
            "node_kind": "hdr_thumbnail",
            "processing_image": _processing_image("hdr-converter"),
            "source_file_location": content_hash,
            "file_extension": file_extension.lower().lstrip('.'),
            "minio_bucket": bucket,
            "gpu_required": False,
            "container_timeout": 120,
            "container_env": {
                "TARGET_FORMAT": "png",
                "MINIO_BUCKET": bucket,
            },
        },
        status=ApiTaskStatus.PENDING,
        max_attempts=1,
        output_namespace="hdr_thumbnail",
    )
    db.add(task)
    db.commit()

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:
        pass

    logging.info(f"Dispatched HDR thumbnail task for cog {cog_id} ({content_hash[:12]}...)")




# ---------------------------------------------------------------------
# SLDPRT → STEP conversion (#8 / launch-day-followup-2 §5.8)
# ---------------------------------------------------------------------





# ---------------------------------------------------------------------
# Point cloud preview (#7 / launch-day-followup-2 §5)
# ---------------------------------------------------------------------

_POINTCLOUD_NATIVE_EXTS = {"pcd", "ply", "xyz"}
_POINTCLOUD_NEEDS_CONVERT_EXTS = {"las", "laz", "e57"}






from datetime import datetime


def _lazy_dispatch_thumbnail(db, cog, current_user) -> bool:
    """Lazy thumbnail backfill (round-12 follow-up). Detect the cog's
    file extension and dispatch the right preview-generation task if
    one is available. Returns True if a task was dispatched, False if
    nothing applies. Capped at 3 retries via
    `processing_metadata.thumbnail_dispatch_retries`."""
    return cogs_shared_service.lazy_dispatch_thumbnail(db, cog, current_user)


def _ensure_raster_thumbnail(db, cog, tenant_id, minio_client) -> Optional[str]:
    """Generate-on-first-request backfill for raster IMAGE cogs (png/jpg/gif/
    bmp/webp). Resolves the cog's content hash + extension and delegates to the
    shared idempotent generator, which stores a small WebP under `thumbnails/`
    in the tenant bucket and records `processing_metadata.thumbnail_path`.

    Returns the thumbnail object key, or None when the cog isn't a raster image
    or generation isn't possible (caller then falls through to the existing
    worker-dispatch path / 404, and the picker uses the full image).
    """
    return cogs_shared_service.ensure_raster_thumbnail(db, cog, tenant_id, minio_client)
































# =============================================================================
# --- DOWNLOAD JOB ENDPOINTS (Async with Progress) ---
# =============================================================================










# =============================================================================
# --- Data Classification Endpoint ---
# =============================================================================



# =============================================================================
# --- Legal Hold Endpoints ---
# =============================================================================


# Restore the pre-split module identity on every class DEFINED here (Pydantic
# request/response models, etc.) so `_model_name` / OpenAPI report them under
# the package (`routers.cogs`) exactly as before the split — the contract
# snapshot compares request_model/response_model by `module:name`.
_pkg_name = __name__.rsplit(".", 1)[0]
for _obj in list(globals().values()):
    if isinstance(_obj, type) and getattr(_obj, "__module__", None) == __name__:
        try:
            _obj.__module__ = _pkg_name
        except Exception:
            pass

# Export EVERY symbol (incl. underscore helpers + imported names) so a
# `from ._shared import *` reproduces the pre-split module namespace in
# each domain sub-module and in the package __init__ aggregator.
__all__ = [__n for __n in list(globals()) if not __n.startswith('__')]
