"""Service layer for `routers/cogs/_shared.py` (openspec:
stack-training-and-prewarm, Gate 2b).

`_shared.py` has no FastAPI endpoints of its own — it is the module every
`routers/cogs/*` sub-module (and several `services/*` modules) imports its
helpers from. This module holds the business logic for the ten helpers that
had inline `db.query(...)` calls; `_shared.py` keeps each function PHYSICALLY
defined under its original name (preserving every external import path:
`from routers.cogs._shared import _find_name_collision`,
`from routers.cogs import _dispatch_thumbnail_task`, etc.) as a thin call
into this module. All data access goes through `crud/cogs_shared.py`.

These are deep internal helpers, not HTTP-boundary handlers — several
(`_copy_file`, `_next_copy_suffix_name` via `_find_name_collision`) already
raise `HTTPException` directly and are called from other routers
(`routers/cogs/assets_cogs_files.py`) that expect that exception to propagate
unchanged. Per the migration rules' behavior-preservation requirement, this
module keeps raising `HTTPException` directly rather than translating to
`services._base.ServiceError` — introducing a translation layer here would
require touching the (out-of-scope, already-migrated) callers in
`assets_cogs_files.py` to catch it.

Heavy/app modules are imported lazily inside functions, matching the
pre-migration code and the function-local-import convention used elsewhere
in this codebase (also what breaks the `_shared.py` <-> here import cycle for
the two dispatch helpers `_lazy_dispatch_thumbnail` still needs from
`_shared.py`: `_dispatch_thumbnail_task` / `_dispatch_hdr_thumbnail_task`,
which have no inline `db.query` and so were left in place there).
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from crud import cogs_shared as shared_crud
from utils.minio_client import get_tenant_bucket_name
from utils.registries import platform_registry

logger = logging.getLogger(__name__)

from services.debug_mode import is_debug_mode


def _scheme() -> str:
    """Storage URL scheme (deployment config, not behaviour). `s3` is the
    primary default; `minio` stays a REAL accepted value for existing
    deployments (STORAGE_SCHEME=minio)."""
    return os.getenv("STORAGE_SCHEME", "s3")


def _storage_schemes() -> tuple:
    """Schemes accepted when parsing a stored storage_path (dual-read).

    `s3` is the primary default; `minio` stays a REAL accepted value so an
    existing deployment with STORAGE_SCHEME=minio keeps its old behaviour.
    Stored values are never rewritten.
    """
    return tuple(dict.fromkeys((_scheme(), "s3", "minio")))


def _storage_object_path(storage_path: str, bucket: str) -> str:
    """Object key for a `s3://bucket/key` (or legacy `minio://bucket/key`) path.

    Dual-read (MinIO->S3 rename, piece P10e-b1): new writes carry `s3://`,
    stored values from before still hold `minio://`. Both are understood here;
    nothing is ever rewritten.
    """
    if not storage_path:
        return ""
    for scheme in _storage_schemes():
        if storage_path.startswith(f"{scheme}://{bucket}/"):
            return storage_path[len(f"{scheme}://{bucket}/"):]
        if storage_path.startswith(f"{scheme}://"):
            after = storage_path[len(f"{scheme}://"):]
            return "/".join(after.split("/")[1:]) if "/" in after else after
    return storage_path


# =============================================================================
# _get_or_create_tenant_root_folder
# =============================================================================


def get_or_create_tenant_root_folder(db: Session, user) -> "models.Cog":  # noqa: F821
    """Return the tenant's root MASTER_FOLDER, creating it on first use.

    Unchanged from the pre-migration inline version — only the `db.query`
    root lookup moved to `crud.cogs_shared.get_tenant_master_root`."""
    import crud
    import models
    import schemas

    root = shared_crud.get_tenant_master_root(db, user.tenant_id)
    if root:
        return root

    # Lazy create. crud.create_cog handles the INSERT + commit.
    new_root = crud.create_cog(
        db=db,
        cog=schemas.CogCreate(
            name="My Files",
            cog_type=models.CogType.MASTER_FOLDER,
            parent_id=None,
        ),
        user_id=user.id,
        tenant_id=user.tenant_id,
    )
    logger.info(
        "Auto-created tenant root folder %d (tenant=%d, owner_user=%d)",
        new_root.id, user.tenant_id, user.id,
    )
    return new_root


# =============================================================================
# _attach_original_paths
# =============================================================================


def attach_original_paths(db: Session, cogs, tenant_id: int) -> None:
    """Attach a human-readable ``original_path`` to each cog in ``cogs``.

    Unchanged algorithm from the pre-migration inline version — only the
    per-level `db.query` in the inner `_load` moved to
    `crud.cogs_shared.get_cog_id_name_parent_pairs`."""
    if not cogs:
        return

    node_cache: dict = {}

    def _load(ids):
        missing = [i for i in ids if i is not None and i not in node_cache]
        while missing:
            rows = shared_crud.get_cog_id_name_parent_pairs(
                db, tenant_id=tenant_id, ids=missing
            )
            found_ids = set()
            next_missing = []
            for cid, name, parent_id in rows:
                node_cache[cid] = (name, parent_id)
                found_ids.add(cid)
                if parent_id is not None and parent_id not in node_cache:
                    next_missing.append(parent_id)
            for cid in missing:
                if cid not in found_ids:
                    node_cache[cid] = (None, None)
            missing = [i for i in next_missing if i not in node_cache]

    _load([getattr(c, "parent_id", None) for c in cogs])

    for cog in cogs:
        parts = []
        seen = set()
        pid = getattr(cog, "parent_id", None)
        while pid is not None and pid not in seen:
            seen.add(pid)
            entry = node_cache.get(pid)
            if not entry:
                break
            name, next_pid = entry
            if name is None:
                break
            parts.append(name)
            pid = next_pid
        cog.original_path = "/" + "/".join(reversed(parts)) if parts else "/"


# =============================================================================
# _find_name_collision
# =============================================================================


def find_name_collision(
    db: Session,
    *,
    name: str,
    parent_id: Optional[int],
    tenant_id: int,
    exclude_cog_id: Optional[int] = None,
) -> Optional["models.Cog"]:  # noqa: F821
    """Look up a non-deleted sibling with the same name in the destination
    folder. Unchanged from the pre-migration inline version — the query
    moved to `crud.cogs_shared.find_name_collision`."""
    return shared_crud.find_name_collision(
        db,
        name=name,
        parent_id=parent_id,
        tenant_id=tenant_id,
        exclude_cog_id=exclude_cog_id,
    )


# =============================================================================
# _copy_file
# =============================================================================


def copy_file(
    db: Session,
    source_cog,
    dest_parent_id: Optional[int],
    new_name: str,
    owner_id: int,
    tenant_id: int,
    minio_client,
):
    """Copy a file cog and its content in MinIO. Unchanged algorithm from the
    pre-migration inline version — every `db.query` moved to
    `crud/cogs_shared.py`."""
    from io import BytesIO

    import models
    from models import ContentBlob

    # Refuse to copy an orphan cog — one with no version, no file row,
    # and therefore no recoverable content. Copying it just creates
    # ANOTHER orphan that 404s on /download forever. (round 14.)
    has_version = source_cog.latest_version_id is not None
    has_file_row = shared_crud.get_any_active_file_for_cog(db, source_cog.id) is not None
    if not has_version and not has_file_row:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This file has no content and cannot be copied. "
                "It looks like an orphan record from a previous failed "
                "upload — please re-upload the source file."
            ),
        )

    # Create new cog record
    new_cog = models.Cog(
        name=new_name,
        cog_type=source_cog.cog_type,
        parent_id=dest_parent_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        is_system_generated=False
    )
    db.add(new_cog)
    db.flush()
    # Gate 24: folder owner gets an OWNER grant (no-op for FILE cogs).
    from resource_permissions import grant_cog_owner_if_folder
    grant_cog_owner_if_folder(db, new_cog)

    # Get active file record from File table (exclude soft-deleted)
    source_file = shared_crud.get_active_file_for_cog(
        db, cog_id=source_cog.id, tenant_id=tenant_id
    )

    # launch-day-followup-2 #4 — when the source has no File row but
    # does have version → ContentBlob (older dedupe-uploaded content),
    # bind the new cog's File row to the same blob instead of producing
    # a Cog with no File and a 404 on /cogs/{id}/download. Content is
    # hash-addressed, so we don't need to re-upload anything in MinIO;
    # the existing blob is reachable from the new File row's
    # storage_path.
    if (not source_file or not source_file.storage_path) and source_cog.latest_version_id:
        version = shared_crud.get_version_by_id(db, source_cog.latest_version_id)
        if version and version.content_id:
            blob = shared_crud.get_content_blob(
                db, content_id=version.content_id, tenant_id=tenant_id
            )
            if blob and blob.storage_path:
                new_file = models.File(
                    cog_id=new_cog.id,
                    tenant_id=tenant_id,
                    user_id=owner_id,
                    name=new_name,
                    storage_path=blob.storage_path,
                    mime_type=blob.mime_type,
                    size_bytes=blob.size_bytes,
                )
                db.add(new_file)
                db.flush()
                # Don't mutate blob.cog_id — both cogs reference the
                # same blob via their File rows; rewriting cog_id
                # would orphan the source's resolution.
                return new_cog

    # Copy file content from MinIO if file record exists
    if source_file and source_file.storage_path:
        try:
            # Parse storage path to get the object path (dual-read: `s3://` is
            # primary, legacy `minio://` rows still resolve; nothing rewritten)
            # Use tenant bucket from s3_client (TPN CS-1 compliant)
            minio_bucket = minio_client.bucket_name
            source_object_path = _storage_object_path(
                source_file.storage_path, minio_bucket)
            # Writer prefix for the new storage_path stamped below (P10d
            # semantics: the deployment's scheme, `s3` by default).
            scheme_prefix = f"{_scheme()}://{minio_bucket}/"

            # Download from MinIO
            response = minio_client.client.get_object(
                bucket_name=minio_client.bucket_name,
                object_name=source_object_path
            )
            file_data = response.read()
            response.close()

            # Generate new storage path
            from routers.cogs._shared import build_folder_path
            dest_folder_path = build_folder_path(
                db=db,
                cog_name="",  # Empty since we just want the folder path
                parent_id=dest_parent_id,
                tenant_id=tenant_id
            )

            # Extract file extension from source file (lowercase, no dot)
            file_extension = ""
            if source_file.name and '.' in source_file.name:
                file_extension = os.path.splitext(source_file.name)[1].lstrip('.').lower()
            elif new_name and '.' in new_name:
                file_extension = os.path.splitext(new_name)[1].lstrip('.').lower()

            # FLAT STORAGE: Files stored as pure SHA256 hash (no extension) in MinIO
            # Extension stored in database for processing tools and downloads
            content_hash = hashlib.sha256(file_data).hexdigest()
            minio_object_name = content_hash  # Pure hash, no extension

            # FX.93 p4b — abuse hash-match hook (services/abuse_hash_match.py).
            # OFF by default via the abuse.hash_match_enabled platform setting
            # and never raises: a match files ONE system AbuseReport, provider
            # errors degrade to a logged non-match. content_hash is already
            # computed above for flat storage, so the hook adds no hashing
            # cost; the new cog id is known (flushed above) and is the target.
            try:
                from services import abuse_hash_match
                abuse_hash_match.scan_upload_hash(
                    db,
                    tenant_id=tenant_id,
                    sha256=content_hash,
                    media_type=source_file.mime_type or "",
                    target_type="file",
                    target_id=str(new_cog.id),
                )
            except Exception as e:  # noqa: BLE001 — belt+braces: the hook
                # never raises, but the copy must not break either way.
                logger.warning(
                    "FX.93 p4b hash-match hook failed for cog=%s: %s",
                    new_cog.id, e)

            # Upload to new location
            file_stream = BytesIO(file_data)
            minio_client.client.put_object(
                bucket_name=minio_client.bucket_name,
                object_name=minio_object_name,
                data=file_stream,
                length=len(file_data),
                content_type=source_file.mime_type or "application/octet-stream"
            )

            # Generate full storage path with scheme (pure hash)
            full_storage_path = f"{scheme_prefix}{minio_object_name}"

            # Create or update ContentBlob record for deduplication tracking
            # Use upsert logic to handle duplicate content hashes
            existing_blob = shared_crud.get_content_blob(
                db, content_id=content_hash, tenant_id=tenant_id
            )

            if existing_blob:
                # Content already exists - update storage path and cog reference
                existing_blob.storage_path = full_storage_path
                existing_blob.cog_id = new_cog.id
                existing_blob.mime_type = source_file.mime_type
                existing_blob.file_extension = file_extension
            else:
                # New content - create ContentBlob
                content_blob = ContentBlob(
                    id=content_hash,
                    tenant_id=tenant_id,
                    size_bytes=len(file_data),
                    mime_type=source_file.mime_type,
                    file_extension=file_extension,
                    storage_path=full_storage_path,
                    cog_id=new_cog.id
                )
                db.add(content_blob)

            # Create new file record
            new_file = models.File(
                cog_id=new_cog.id,
                tenant_id=tenant_id,
                user_id=owner_id,
                name=new_name,
                storage_path=full_storage_path,
                mime_type=source_file.mime_type,
                size_bytes=source_file.size_bytes
            )
            db.add(new_file)
            db.flush()

        except Exception as e:
            if is_debug_mode():
                logger.error(f"Error copying file in MinIO: {e}")
            else:
                logger.error("Error copying file in MinIO")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to copy file content"
            )

    return new_cog


# =============================================================================
# _copy_folder_recursive
# =============================================================================


def copy_folder_recursive(
    db: Session,
    source_cog,
    dest_parent_id: Optional[int],
    new_name: str,
    owner_id: int,
    tenant_id: int,
    minio_client,
):
    """Recursively copy a folder and all its contents. Unchanged algorithm
    from the pre-migration inline version — the children lookup moved to
    `crud.cogs_shared.get_active_children_by_owner`."""
    import models

    # Create new folder cog
    new_folder = models.Cog(
        name=new_name,
        cog_type=source_cog.cog_type,
        parent_id=dest_parent_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        is_system_generated=False
    )
    db.add(new_folder)
    db.flush()
    # Gate 24: folder owner gets an OWNER grant.
    from resource_permissions import grant_cog_owner_if_folder
    grant_cog_owner_if_folder(db, new_folder)

    # Note: Folders are virtual (database-only). No MinIO folder creation needed.

    # Get all children of source folder
    children = shared_crud.get_active_children_by_owner(
        db, parent_id=source_cog.id, owner_id=owner_id
    )

    # Recursively copy each child
    for child in children:
        if child.cog_type in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
            copy_folder_recursive(
                db=db,
                source_cog=child,
                dest_parent_id=new_folder.id,
                new_name=child.name,
                owner_id=owner_id,
                tenant_id=tenant_id,
                minio_client=minio_client
            )
        elif child.cog_type == models.CogType.FILE:
            copy_file(
                db=db,
                source_cog=child,
                dest_parent_id=new_folder.id,
                new_name=child.name,
                owner_id=owner_id,
                tenant_id=tenant_id,
                minio_client=minio_client
            )

    return new_folder


# =============================================================================
# _dispatch_pointcloud_preview_task
# =============================================================================


def dispatch_pointcloud_preview_task(db, cog_id, content_hash, file_extension, tenant_id, user_id):
    """Dispatch a worker task that converts a point cloud (pcd/las/laz/
    xyz/e57) to binary PLY plus a PNG thumbnail. Unchanged from the
    pre-migration inline version — the cog lookup moved to
    `crud.cogs_shared.get_cog_by_id`."""
    from models import ApiTaskQueue, ApiTaskStatus

    registry = platform_registry()
    service_api_key = os.getenv("PLATFORM_SERVICE_API_KEY", "")
    bucket = get_tenant_bucket_name(tenant_id, db)
    ext = file_extension.lower().lstrip('.')

    cog = shared_crud.get_cog_by_id(db, cog_id)
    if cog is not None:
        meta = dict(cog.processing_metadata or {})
        meta["pointcloud_conversion_status"] = "pending"
        meta["pointcloud_conversion_started_at"] = datetime.utcnow().isoformat()
        meta.pop("pointcloud_conversion_error", None)
        cog.processing_metadata = meta
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(cog, "processing_metadata")
        db.commit()

    task = ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"pointcloud-preview-{cog_id}",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=5,
        task_payload={
            "node_kind": "pointcloud_preview",
            "processing_image": f"{registry}/modtim/pointcloud-preview:latest",
            "source_file_location": content_hash,
            "minio_bucket": bucket,
            "gpu_required": False,
            "container_timeout": 240,  # large LAZ files need a couple of
                                        # minutes to decompress + write.
            "container_env": {
                "CONTENT_HASH": content_hash,
                "FILE_EXTENSION": ext,
                "COG_ID": str(cog_id),
                "TENANT_ID": str(tenant_id),
                "MINIO_BUCKET": bucket,
                "API_URL": os.getenv("API_BASE_URL", "http://api:80"),
                "API_KEY": service_api_key,
            },
        },
        status=ApiTaskStatus.PENDING,
        max_attempts=1,
        output_namespace="pointcloud_preview",
    )
    db.add(task)
    db.commit()

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:
        pass

    logging.info(f"Dispatched point cloud preview for cog {cog_id} ({content_hash[:12]}...)")


# =============================================================================
# _dispatch_sldprt_conversion_task
# =============================================================================


def dispatch_sldprt_conversion_task(db, cog_id, content_hash, tenant_id, user_id):
    """Dispatch a worker task that converts a .sldprt to STEP via FreeCAD.
    Unchanged from the pre-migration inline version — the cog lookup moved to
    `crud.cogs_shared.get_cog_by_id`."""
    from models import ApiTaskQueue, ApiTaskStatus

    registry = platform_registry()
    service_api_key = os.getenv("PLATFORM_SERVICE_API_KEY", "")
    bucket = get_tenant_bucket_name(tenant_id, db)

    # Mark the cog as conversion-pending so the FE can render a "converting…"
    # state immediately instead of "no preview available".
    cog = shared_crud.get_cog_by_id(db, cog_id)
    if cog is not None:
        meta = dict(cog.processing_metadata or {})
        meta["sldprt_conversion_status"] = "pending"
        meta["sldprt_conversion_started_at"] = datetime.utcnow().isoformat()
        meta.pop("sldprt_conversion_error", None)
        cog.processing_metadata = meta
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(cog, "processing_metadata")
        db.commit()

    task = ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"sldprt-convert-{cog_id}",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=5,
        task_payload={
            "node_kind": "sldprt_convert",
            "processing_image": f"{registry}/modtim/sldprt-converter:latest",
            "source_file_location": content_hash,
            "minio_bucket": bucket,
            "gpu_required": False,
            "container_timeout": 300,  # FreeCAD startup is slow; complex
                                        # parts can take 30-60s to convert.
            "container_env": {
                "CONTENT_HASH": content_hash,
                "COG_ID": str(cog_id),
                "TENANT_ID": str(tenant_id),
                "MINIO_BUCKET": bucket,
                "API_URL": os.getenv("API_BASE_URL", "http://api:80"),
                "API_KEY": service_api_key,
            },
        },
        status=ApiTaskStatus.PENDING,
        max_attempts=1,
        output_namespace="sldprt_step",
    )
    db.add(task)
    db.commit()

    try:
        import redis
        r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        r.publish("task:available:CPU", "1")
    except Exception:
        pass

    logging.info(f"Dispatched SLDPRT conversion for cog {cog_id} ({content_hash[:12]}...)")


# =============================================================================
# _lazy_dispatch_thumbnail
# =============================================================================


def lazy_dispatch_thumbnail(db, cog, current_user) -> bool:
    """Lazy thumbnail backfill (round-12 follow-up). Unchanged from the
    pre-migration inline version — the version lookup moved to
    `crud.cogs_shared.get_version_by_id`.

    `_dispatch_thumbnail_task` / `_dispatch_hdr_thumbnail_task` have no
    inline `db.query` and stayed in `routers/cogs/_shared.py`; imported
    locally here to avoid a module-import cycle with that module (which
    imports this service at top level)."""
    from routers.cogs._shared import _dispatch_thumbnail_task, _dispatch_hdr_thumbnail_task

    cog_name = (cog.name or "").lower()
    if "." not in cog_name:
        return False
    ext = cog_name.rsplit(".", 1)[-1]
    meta = dict(cog.processing_metadata or {})
    retries = int(meta.get("thumbnail_dispatch_retries") or 0)
    if retries >= 3:
        return False

    # Resolve the source content_hash via the latest version chain.
    content_hash = None
    try:
        if cog.latest_version_id:
            version = shared_crud.get_version_by_id(db, cog.latest_version_id)
            if version and version.content_id:
                content_hash = version.content_id
    except Exception:
        return False
    if not content_hash:
        return False

    THUMBNAIL_3D = {'glb', 'gltf', 'obj', 'fbx', 'ply', 'stl', 'step', 'stp', 'usd', 'usda', 'usdc', 'usdz'}
    HDR = {'exr', 'hdr'}
    POINTCLOUD = {'pcd', 'las', 'laz', 'xyz', 'e57'}

    try:
        if ext in THUMBNAIL_3D:
            _dispatch_thumbnail_task(db, cog.id, content_hash, ext, cog.tenant_id, current_user.id)
        elif ext in HDR:
            _dispatch_hdr_thumbnail_task(db, cog.id, content_hash, ext, cog.tenant_id, current_user.id)
        elif ext in POINTCLOUD:
            dispatch_pointcloud_preview_task(db, cog.id, content_hash, ext, cog.tenant_id, current_user.id)
        elif ext == 'sldprt':
            # SLDPRT needs a 2-step chain: convert to STEP, then
            # thumbnail the STEP. If the STEP already exists, jump
            # straight to thumbnail-gen on it. Otherwise dispatch the
            # SLDPRT conversion; its callback chains to thumbnail-gen
            # on success.
            step_object = (cog.processing_metadata or {}).get('sldprt_step_object')
            if step_object:
                _dispatch_thumbnail_task(db, cog.id, step_object, 'step', cog.tenant_id, current_user.id)
            else:
                sldprt_status = ((cog.processing_metadata or {}).get('sldprt_conversion_status') or '').lower()
                if sldprt_status not in ('pending',):
                    dispatch_sldprt_conversion_task(db, cog.id, content_hash, cog.tenant_id, current_user.id)
                # else: pending — callback will chain when STEP lands
        else:
            return False
    except Exception as e:
        logging.warning(f"Lazy thumbnail dispatch failed for cog {cog.id}: {e}")
        return False

    cog.processing_metadata = {
        **(cog.processing_metadata or {}),
        "thumbnail_dispatch_retries": retries + 1,
    }
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(cog, "processing_metadata")
    db.commit()
    return True


# =============================================================================
# _ensure_raster_thumbnail
# =============================================================================


def ensure_raster_thumbnail(db, cog, tenant_id, minio_client) -> Optional[str]:
    """Generate-on-first-request backfill for raster IMAGE cogs. Unchanged
    from the pre-migration inline version — the version lookup moved to
    `crud.cogs_shared.get_version_by_id`."""
    try:
        from utils.thumbnails import (
            is_raster_thumbnailable, ensure_raster_thumbnail_for_blob,
        )
    except Exception:
        return None

    name = (cog.name or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if not is_raster_thumbnailable(ext):
        return None

    content_hash = None
    try:
        if cog.latest_version_id:
            version = shared_crud.get_version_by_id(db, cog.latest_version_id)
            if version and version.content_id:
                content_hash = version.content_id
    except Exception:
        return None
    if not content_hash:
        return None

    return ensure_raster_thumbnail_for_blob(
        db, cog_id=cog.id, content_hash=content_hash,
        tenant_id=tenant_id, ext=ext, minio_client=minio_client,
    )


# =============================================================================
# _maybe_audit_xtenant_block
# =============================================================================


def maybe_audit_xtenant_block(
    db: Session,
    current_user,
    cog_id: Optional[int],
    request=None,
) -> None:
    """data-files-launch-bulletproof §0 — when a platform admin gets a 403
    from a cogs.py route, check whether the cog they tried to access lives
    in another tenant and emit a debounced audit event. Unchanged from the
    pre-migration inline version — the cog lookup (intentionally
    tenant-UNSCOPED, so a cross-tenant probe can be detected at all) moved
    to `crud.cogs_shared.get_cog_by_id`. Best-effort and never raises."""
    import audit

    try:
        if not (current_user and current_user.is_platform_admin):
            return
        if cog_id is None:
            return
        cog = shared_crud.get_cog_by_id(db, cog_id)
        if cog is None or cog.tenant_id == current_user.tenant_id:
            return
        audit.log_cross_tenant_data_access_blocked(
            db=db,
            admin_user_id=current_user.id,
            target_tenant_id=cog.tenant_id,
            request=request,
            resource_type="cog",
            resource_id=cog_id,
            endpoint_path=request.url.path if request else None,
            method=request.method if request else None,
        )
    except Exception:
        # Audit emission must never break the 403 response path.
        logger.exception("audit emission failed in maybe_audit_xtenant_block")
