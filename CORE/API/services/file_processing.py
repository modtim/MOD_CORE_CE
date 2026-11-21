"""In-process file-IO for uploads + downloads.

Replaces the file-hasher and zip-download worker containers with
direct API execution. Why: those tasks are pure platform IO against
MinIO. They don't need GPU, sandbox, tenant container runtime, or
customer-private code execution. Dispatching them to workers adds:

  - An STS handoff (API mints temp creds, worker injects them as env
    vars on the task container). This is the source of the
    ``AccessDenied request_id=18B4D96F0283F531`` failure class
    observed 2026-06-01 task=6575 on worker-example-host — see
    routers/worker_tasks.py:1008 for the same failure mode from
    "round 14".
  - An image pull cycle (~3-5s + occasional Harbor 401s).
  - A worker-claim race (any worker on the queue can grab it; a
    sick worker silently fails the upload).

Running in-process gives the work the API service's already-warm
MinIO client (Vault-loaded service credentials, no STS handoff) and
bypasses every failure mode that comes from cred shipping. For
TB-scale files the bottleneck is MinIO IO, not the hash — and MinIO
is on the same docker network as the API.

Tasks that legitimately stay on workers (build sidecars, tenant
processing nodes, real customer code) still flow through
``/workers/tasks/<id>/credentials`` and the worker_tasks router's
STS-handoff path.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from minio.commonconfig import CopySource
from minio.datatypes import Part
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# 8 MB streaming chunks. Same as the legacy file-hasher container —
# big enough that hash + network throughput stays IO-bound; small
# enough that a 4 GB upload doesn't pin 4 GB of RAM.
_HASH_CHUNK_BYTES = 8 * 1024 * 1024

# 32 KB chunks for zip extraction reads. Smaller because we're
# writing to a tempfile and the bottleneck is disk + zipfile's
# DEFLATE/STORE encoder, not network throughput.
_ZIP_CHUNK_BYTES = 32 * 1024


def hash_and_register_upload(
    *,
    db: Session,
    upload_id: str,
    filename: str,
    parent_id: int,
    file_size: int,
    content_type: str,
    tenant_id: int,
    user_id: int,
    s3_upload_id: str = "",
    parts_json: str = "[]",
    temp_object: str = "",
) -> Dict[str, Any]:
    """Hash the temp MinIO object, dedup, rename to content-hash,
    create the ContentBlob/Cog/Version/File DB records, and dispatch
    any conversion tasks (thumbnail / GLB / HDR / SLDPRT) the same
    way the legacy worker container did.

    Returns a result dict with ``status``, ``content_hash``,
    ``file_size``, ``is_dedup``, ``cog_id``, ``duration_seconds``.

    Raises on unrecoverable failure. Callers should catch and surface
    the user-facing error.

    Replaces ``CORE/DockerContainers/file-hasher/hash_and_register.py``.
    The legacy container's HTTP self-calls to ``/cogs/service/register-
    uploaded-file`` are now inline DB ops — same shape, just no
    worker-claim → STS-handoff → image-pull → container-start
    overhead.
    """
    started = datetime.utcnow()
    # GSEC R-032: the caller may supply the already-validated namespaced temp
    # key (uploads/temp/<user_id>/<upload_id>); default to the legacy flat key.
    temp_object = temp_object or f"uploads/temp/{upload_id}"

    # Use the API's already-initialised service-account MinIO client.
    # No STS handoff. No env vars. No worker. When the tenant has an ACTIVE
    # BYO-S3 StorageConnection, MinioClient resolves BOTH the client (bound to
    # the tenant's own endpoint) AND the bucket name to that connection;
    # otherwise it is the house default tenant-{id}-files. The bucket MUST come
    # from the SAME resolution as the client — a BYO-bound client with the
    # hardcoded house bucket name points at a nonexistent bucket on the tenant
    # endpoint (NoSuchBucket) and confirm-upload fails.
    from utils.minio_client import MinioClient
    _mc_obj = MinioClient(tenant_id=tenant_id)
    mc = _mc_obj.client
    bucket = _mc_obj.bucket_name

    # Step 1: multipart assembly (if applicable).
    if s3_upload_id:
        import json as _json
        try:
            parts = _json.loads(parts_json or "[]")
            if parts:
                minio_parts = [
                    Part(p["part_number"], p["etag"])
                    for p in sorted(parts, key=lambda x: x["part_number"])
                ]
                # _complete_multipart_upload is the same private call
                # the legacy container used; minio-py exposes it for
                # exactly this stitch-together case.
                mc._complete_multipart_upload(  # type: ignore[attr-defined]
                    bucket, temp_object, s3_upload_id, minio_parts,
                )
                logger.info(
                    "hash_and_register: assembled %d multipart parts for upload=%s",
                    len(parts), upload_id,
                )
        except Exception as e:
            raise RuntimeError(f"multipart assembly failed: {e}") from e

    # Step 2: streaming SHA-256.
    sha256 = hashlib.sha256()
    actual_size = 0
    try:
        response = mc.get_object(bucket, temp_object)  # type: ignore[attr-defined]
        try:
            for chunk in response.stream(_HASH_CHUNK_BYTES):
                sha256.update(chunk)
                actual_size += len(chunk)
        finally:
            response.close()
            response.release_conn()
        content_hash = sha256.hexdigest()
    except Exception as e:
        raise RuntimeError(f"streaming hash failed: {e}") from e

    # FX.93 p4b — abuse hash-match hook (services/abuse_hash_match.py).
    # OFF by default via the abuse.hash_match_enabled platform setting and
    # never raises: a match files ONE system AbuseReport, provider errors
    # degrade to a logged non-match. Runs BEFORE the dedup check so a
    # dedup'd re-upload of known-bad content is still scanned. The
    # Cog/File rows don't exist yet at this point, so the report target is
    # the upload itself (target_type 'file' — 'upload' is not a valid
    # abuse_reports.TARGET_TYPES value and create_report would reject it).
    try:
        from services import abuse_hash_match
        abuse_hash_match.scan_upload_hash(
            db,
            tenant_id=tenant_id,
            sha256=content_hash,
            media_type=content_type or "",
            target_type="file",
            target_id=upload_id,
        )
    except Exception as e:  # noqa: BLE001 — belt+braces: the hook never
        # raises, but the upload path must not break either way.
        logger.warning(
            "FX.93 p4b hash-match hook failed for upload=%s: %s",
            upload_id, e)

    # Step 3: dedup check — does this content_hash already exist as
    # an object in the bucket?
    is_dedup = False
    try:
        mc.stat_object(bucket, content_hash)  # type: ignore[attr-defined]
        is_dedup = True
        mc.remove_object(bucket, temp_object)  # type: ignore[attr-defined]
        logger.info(
            "hash_and_register: dedup hit, dropped temp upload=%s "
            "(content_hash=%s...)",
            upload_id, content_hash[:16],
        )
    except Exception:
        is_dedup = False

    # Q.2: the size declared at request-upload can lie; enforce the cap on the
    # bytes actually received. Nothing is registered yet, so drop the temp
    # object (as the dedup path does) and fail the task.
    if not is_dedup:
        from utils.usage_enforcement import storage_quota_ok
        if not storage_quota_ok(db, tenant_id, actual_size):
            mc.remove_object(bucket, temp_object)  # type: ignore[attr-defined]
            raise RuntimeError(
                f"Storage limit exceeded: upload of {actual_size} bytes is over the "
                "tenant's storage cap. Delete old files to free space."
            )

    # Step 4: rename temp → content_hash (non-dedup path).
    if not is_dedup:
        try:
            mc.copy_object(  # type: ignore[attr-defined]
                bucket, content_hash,
                CopySource(bucket, temp_object),
            )
            mc.remove_object(bucket, temp_object)  # type: ignore[attr-defined]
        except Exception as e:
            raise RuntimeError(
                f"content-hash rename failed: {e}"
            ) from e

    # Step 5: register in DB. Same shape as the legacy
    # /cogs/service/register-uploaded-file endpoint produces — we
    # reuse it directly so the auto-thumbnail / GLB / HDR / SLDPRT
    # dispatch logic doesn't drift.
    file_extension = ""
    if filename and "." in filename:
        file_extension = filename.rsplit(".", 1)[-1].lower()
    cog_id = _register_uploaded_file_inline(
        db=db,
        content_hash=content_hash,
        filename=filename,
        parent_id=parent_id,
        file_size=actual_size,
        content_type=content_type,
        file_extension=file_extension,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    duration = (datetime.utcnow() - started).total_seconds()
    logger.info(
        "hash_and_register: %s -> %s... (%.1fs, %d MB, dedup=%s, cog_id=%s)",
        filename, content_hash[:16], duration,
        int(actual_size / (1024 * 1024)),
        is_dedup, cog_id,
    )
    return {
        "status": "success",
        "content_hash": content_hash,
        "file_size": actual_size,
        "is_dedup": is_dedup,
        "duration_seconds": duration,
        "cog_id": cog_id,
        "outputs": [{
            "content_hash": content_hash,
            "file_name": filename,
            "file_extension": file_extension,
            "size_bytes": actual_size,
        }],
    }


def _register_uploaded_file_inline(
    *,
    db: Session,
    content_hash: str,
    filename: str,
    parent_id: int,
    file_size: int,
    content_type: str,
    file_extension: str,
    tenant_id: int,
    user_id: int,
) -> int:
    """In-process equivalent of POST /cogs/service/register-uploaded-file.

    Pulled out so ``hash_and_register_upload`` doesn't have to round-
    trip back to its own HTTP API. Returns the new cog_id.

    Stays in lockstep with the router endpoint's logic — when that
    one changes (auto-dispatch flags, new file types, etc.), update
    here too. See ``routers/cogs.py::register_uploaded_file``.
    """
    import models
    import crud
    import schemas

    storage_scheme = os.getenv("STORAGE_SCHEME", "s3")
    # BYO-S3: record the tenant's own bucket in storage_path so the download
    # parser reconstructs the correct object key against the BYO endpoint.
    # Falls back to the house default when no BYO connection is active.
    from utils import storage as _storage
    _cfg = _storage.resolve_backend(tenant_id, db)
    bucket = _cfg.bucket or f"tenant-{tenant_id}-files"
    storage_path = f"{storage_scheme}://{bucket}/{content_hash}"

    # ContentBlob (dedup-aware)
    content_blob = db.query(models.ContentBlob).filter(
        models.ContentBlob.id == content_hash,
        models.ContentBlob.tenant_id == tenant_id,
    ).first()
    is_new_blob = content_blob is None
    if not content_blob:
        content_blob = models.ContentBlob(
            id=content_hash,
            tenant_id=tenant_id,
            size_bytes=file_size,
            mime_type=content_type,
            file_extension=file_extension,
            storage_path=storage_path,
        )
        db.add(content_blob)
        db.flush()

    # Cog
    cog = crud.create_cog(
        db=db,
        cog=schemas.CogCreate(
            name=filename,
            cog_type=models.CogType.FILE,
            parent_id=parent_id,
        ),
        user_id=user_id,
        tenant_id=tenant_id,
    )
    content_blob.cog_id = cog.id
    db.add(content_blob)
    db.flush()

    # Version
    new_version = models.Version(
        cog_id=cog.id,
        tenant_id=tenant_id,
        content_id=content_blob.id,
        version_number=1,
        name="v1",
        author_user_id=user_id,
    )
    db.add(new_version)
    db.flush()
    cog.latest_version_id = new_version.id

    # File
    db.add(models.File(
        tenant_id=tenant_id,
        user_id=user_id,
        cog_id=cog.id,
        name=filename,
        size_bytes=file_size,
        mime_type=content_type,
        storage_path=storage_path,
    ))
    db.commit()

    # remove-minio-versitygw Gate 4 (4.1, D5) — API-side storage accounting.
    # This is the primary confirm-upload finalize path; previously the counter
    # was only corrected by the nightly reconcile loop. Increment it live so
    # `tenants.storage_used_bytes` is accurate immediately after upload. Only
    # add bytes when a NEW content_blobs row was created above — a dedup'd
    # upload onto an existing hash adds nothing (the blob already counts).
    # Non-fatal.
    # CE carve: egress_metering is paid and not shipped in CE; CE skips the
    # live counter quietly (no ERROR traceback per upload).
    try:
        from utils.paid_import import optional_paid
        _egress_metering = optional_paid("services.egress_metering")
        if _egress_metering is None:
            logger.debug("storage accounting skipped (services.egress_metering not in this edition)")
        else:
            _egress_metering.apply_storage_change(
                db, tenant_id, new_blob_bytes=file_size if is_new_blob else 0)
    except Exception:
        logger.exception("storage accounting failed for cog=%s", cog.id)

    # Auto-dispatch downstream conversion tasks. These DO go to
    # workers (legitimate processing needs) — they handle the
    # STS handoff for themselves. We just kick them off the way
    # the legacy register endpoint did.
    ext = file_extension.lower().lstrip(".")
    THUMBNAIL_EXTS = {"glb", "gltf", "obj", "fbx", "ply", "stl",
                      "step", "stp", "usd", "usda", "usdc", "usdz"}
    USD_EXTS = {"usd", "usda", "usdc", "usdz"}
    HDR_EXTS = {"exr", "hdr"}
    SLDPRT_EXTS = {"sldprt"}
    try:
        from routers.cogs import (
            _dispatch_thumbnail_task,
            _dispatch_glb_conversion_task,
            _dispatch_hdr_thumbnail_task,
        )
        if ext in THUMBNAIL_EXTS:
            try:
                _dispatch_thumbnail_task(db, cog.id, content_hash, file_extension, tenant_id, user_id)
            except Exception as e:
                logger.warning("thumbnail dispatch failed for cog=%s: %s", cog.id, e)
        if ext in USD_EXTS:
            try:
                _dispatch_glb_conversion_task(db, cog.id, content_hash, file_extension, tenant_id, user_id)
            except Exception as e:
                logger.warning("USD->GLB dispatch failed for cog=%s: %s", cog.id, e)
        if ext in HDR_EXTS:
            try:
                _dispatch_hdr_thumbnail_task(db, cog.id, content_hash, file_extension, tenant_id, user_id)
            except Exception as e:
                logger.warning("HDR thumbnail dispatch failed for cog=%s: %s", cog.id, e)
    except ImportError as e:
        # Auto-dispatch is best-effort; a missing helper shouldn't fail upload.
        logger.warning("auto-dispatch helpers unavailable: %s", e)

    # Precompute a small raster PREVIEW thumbnail in-process (no worker needed —
    # Pillow makes a 256px WebP directly from the just-stored object). This warms
    # the file-picker cache at upload time so the first folder open is already
    # fast. Best-effort + idempotent; a failure here never fails the upload.
    try:
        from utils.thumbnails import is_raster_thumbnailable, ensure_raster_thumbnail_for_blob
        if is_raster_thumbnailable(ext):
            ensure_raster_thumbnail_for_blob(
                db, cog_id=cog.id, content_hash=content_hash,
                tenant_id=tenant_id, ext=ext,
            )
    except Exception as e:
        logger.warning("raster thumbnail precompute failed for cog=%s: %s", cog.id, e)

    return cog.id


def build_zip_archive(
    *,
    db: Session,
    tenant_id: int,
    folder_id: Optional[int] = None,
    cog_ids: Optional[List[int]] = None,
    download_job_id: Optional[str] = None,
    progress_cb: Optional[Any] = None,
    current_user=None,
    watermark_policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a zip archive of the given folder OR cog_ids, upload it
    to MinIO's downloads/temp/ area, and return the object path so
    the caller can hand back a presigned URL.

    Returns dict with ``zip_path``, ``zip_size_bytes``,
    ``files_count``, ``folder_name``.

    Replaces ``CORE/DockerContainers/zip-download/zip_download.py``.
    Same logic, no worker, no STS handoff, no image pull. The legacy
    container's HTTP self-calls to
    ``/cogs/service/cogs/files`` / ``/cogs/service/folder/{id}/files``
    become in-process DB queries.

    ``progress_cb`` is an optional callable
    ``progress_cb(stage, processed, current_file)`` — used to write
    to the DownloadJob row from the caller. The legacy container
    PATCHed ``/download-jobs/{id}`` for the same effect.
    """
    import models

    started = datetime.utcnow()

    # 1. Resolve files to include.
    files_info, folder_name = _resolve_zip_inputs(
        db=db, tenant_id=tenant_id,
        folder_id=folder_id, cog_ids=cog_ids,
    )
    if not files_info:
        raise ValueError("no files to zip")

    # 2. MinIO client (service account).
    from utils.minio_client import MinioClient
    _mc_obj = MinioClient(tenant_id=tenant_id)
    mc = _mc_obj.client
    bucket = _mc_obj.bucket_name

    # 3. Build the zip on local disk (cheaper than streaming via
    # SpooledTemporaryFile when files are ~MB-GB scale). The legacy
    # container did the same.
    if progress_cb:
        try:
            progress_cb("downloading", 0, None)
        except Exception:
            pass

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as zip_tmp:
        zip_path_local = zip_tmp.name
    try:
        # FX.162.p5d — CE carve: services/watermark_delivery is paid-only
        # (manifest-denied), so the zip watermark layer does not exist on CE.
        # The stand-ins keep the fail-closed handlers below valid while
        # `wm_on` (below) forces the byte-identical unwatermarked build.
        from utils.paid_import import optional_paid
        _wm_del = optional_paid("services.watermark_delivery")
        if _wm_del is None:
            class WatermarkBlockError(Exception):  # never raised on CE
                pass

            def invisible_watermark_enabled(db):  # noqa: CE stand-in
                return False

            def label_requires_watermark(*a, **k):  # noqa: CE stand-in
                return False

            prepare_watermark_bytes = None
        else:
            WatermarkBlockError = _wm_del.WatermarkBlockError
            invisible_watermark_enabled = _wm_del.invisible_watermark_enabled
            label_requires_watermark = _wm_del.label_requires_watermark
            prepare_watermark_bytes = _wm_del.prepare_watermark_bytes

        # GY.V1c: when the watermark feature is on, labelled image members
        # are watermarked in place before being written to the zip. Non-image
        # or unlabelled members pass through byte-identical (same path as
        # today). A labelled member that CANNOT be watermarked under
        # unsupported_action='block' (or whose embedding failed) raises and
        # aborts the whole build — fail-closed, no unmarked member ships.
        # FX.162.p5d — CE carve: no watermark module -> the layer is off even
        # if a caller somehow still passes a policy (byte-identical zip).
        wm_on = (_wm_del is not None and bool(watermark_policy)
                 and current_user is not None)
        wm_min_label = (watermark_policy or {}).get("min_label", "confidential")
        wm_unsupported = (watermark_policy or {}).get("unsupported_action", "allow")
        wm_watermarked = 0
        # GY.V2 R-133: per-member label map for the CLASSIFICATION.txt
        # marking file. Members at/above `internal` threshold are recorded;
        # an all-public/internal (or all-unlabelled) zip adds NO file and
        # stays byte-identical to today.
        from utils.paid_import import optional_paid
        _dc = optional_paid("services.data_classification")
        # 1788 CE carve: the taxonomy is paid; with it absent the CLASSIFICATION.txt
        # marking layer is off (no member labels are recorded -> no marking file,
        # byte-identical zip).
        _MARK_THRESH_RANK = _dc.rank("internal") if _dc is not None else None
        _member_labels: list = []  # [(relative_path, normalized_level)]

        with zipfile.ZipFile(zip_path_local, "w", zipfile.ZIP_STORED) as zipf:
            for idx, fi in enumerate(files_info, 1):
                minio_path = fi.get("minio_path", "")
                relative_path = fi.get("relative_path") or fi.get("name") or f"file_{idx}"
                if not minio_path:
                    continue
                if progress_cb:
                    try:
                        progress_cb("downloading", idx - 1, relative_path)
                    except Exception:
                        pass
                try:
                    resp = mc.get_object(bucket, minio_path)  # type: ignore[attr-defined]
                    # Stream object into zip without staging on disk.
                    # ZipFile.writestr accepts bytes; for large files
                    # use a temp file. Threshold: 16 MB — below that,
                    # in-memory is faster.
                    with tempfile.NamedTemporaryFile(delete=False) as t:
                        tmp_inner = t.name
                        for chunk in resp.stream(_ZIP_CHUNK_BYTES):
                            t.write(chunk)
                    resp.close()
                    resp.release_conn()
                    try:
                        if wm_on and label_requires_watermark(
                                fi.get("data_classification"), wm_min_label):
                            with open(tmp_inner, "rb") as _rf:
                                _raw = _rf.read()
                            res = prepare_watermark_bytes(
                                label=fi.get("data_classification"),
                                content_type=fi.get("mime_type"),
                                raw=_raw,
                                tenant_id=tenant_id,
                                subject_id=current_user.id,
                                object_key=minio_path,
                                min_label=wm_min_label,
                                unsupported_action=wm_unsupported,
                                subject_label=f"user:{current_user.id}",
                                # GY.V1d — labels.invisible_watermark_enabled
                                # (default True — preserves the pre-V1d
                                # always-on invisible layer).
                                invisible_enabled=invisible_watermark_enabled(db),
                            )
                            if res["block"]:
                                raise WatermarkBlockError(
                                    res.get("reason") or "watermark required",
                                    object_key=minio_path)
                            if res["content"] != _raw:
                                with open(tmp_inner, "wb") as _wf:
                                    _wf.write(res["content"])
                                wm_watermarked += 1
                        zipf.write(tmp_inner, arcname=relative_path)
                        # GY.V2 R-133: record this member's label if it is
                        # marked above the public/internal floor.
                        _ml = fi.get("data_classification")
                        if _ml and _dc is not None:
                            try:
                                _mn = _dc.normalize(_ml)
                            except _dc.UnknownClassificationError:
                                _mn = "ITAR"  # fail closed
                            if _dc.rank(_mn) > _MARK_THRESH_RANK:
                                _member_labels.append((relative_path, _mn))
                    finally:
                        try:
                            os.unlink(tmp_inner)
                        except FileNotFoundError:
                            pass
                except WatermarkBlockError:
                    raise  # GY.V1c fail-closed — must not be swallowed below
                except Exception as e:
                    logger.warning(
                        "build_zip: skipping %s: %s", minio_path, e,
                    )
                    continue

        # GY.V2 R-133 — top-level CLASSIFICATION.txt marking file. Written
        # ONLY when at least one member carries a label above
        # public/internal; an unlabelled (or all-public/internal) zip stays
        # byte-identical to today. Lists the highest label in the archive
        # plus the per-member labels.
        _zip_max_label = None
        if _member_labels and _dc is not None:
            _zip_max_label = _dc.max_level([lvl for _p, lvl in _member_labels])
            _top = _dc.max_level([lvl for _p, lvl in _member_labels])
            _lines = [
                "MOD CLASSIFICATION MARKING",
                "=========================",
                f"Highest classification in this archive: {_top}",
                "",
                "Handle the contents of this archive according to the",
                "markings below and the tenant's information-handling",
                "policy. Distribution of classified content is controlled.",
                "",
                "Per-member labels:",
            ]
            for _p, _lvl in sorted(_member_labels):
                _lines.append(f"  [{_lvl}] {_p}")
            _lines.append("")
            with zipfile.ZipFile(zip_path_local, "a", zipfile.ZIP_STORED) as _zf:
                _zf.writestr("CLASSIFICATION.txt", "\n".join(_lines))

        zip_size = os.path.getsize(zip_path_local)
        if progress_cb:
            try:
                progress_cb("creating_zip", len(files_info), None)
            except Exception:
                pass

        # 4. Upload to MinIO temp downloads area.
        safe_name = "".join(
            c if c.isalnum() or c in "-_." else "_" for c in folder_name
        )
        zip_object = (
            f"downloads/temp/"
            f"{safe_name}_{int(datetime.utcnow().timestamp())}.zip"
        )
        mc.fput_object(  # type: ignore[attr-defined]
            bucket, zip_object, zip_path_local,
            content_type="application/zip",
        )
    finally:
        try:
            os.unlink(zip_path_local)
        except FileNotFoundError:
            pass

    if progress_cb:
        try:
            progress_cb("completed", len(files_info), None)
        except Exception:
            pass

    duration = (datetime.utcnow() - started).total_seconds()
    logger.info(
        "build_zip: %d files -> %s (%.1f MB, %.1fs)",
        len(files_info), zip_object,
        zip_size / (1024 * 1024), duration,
    )
    return {
        "status": "success",
        "zip_path": f"{bucket}/{zip_object}",
        "zip_object": zip_object,
        "zip_size_bytes": zip_size,
        "files_count": len(files_info),
        "folder_name": folder_name,
        "duration_seconds": duration,
        "watermarked_members": wm_watermarked,  # GY.V1c
        # GY.V2 R-133: archive marking for the delivery headers.
        # zip_max_label = highest member label (None when no member is
        # labelled above public/internal, i.e. no CLASSIFICATION.txt).
        "zip_max_label": _zip_max_label,
        "outputs": [{
            "file_path": zip_object,
            "filename": f"{safe_name}.zip",
            "size_bytes": zip_size,
        }],
    }


def _resolve_zip_inputs(
    *,
    db: Session,
    tenant_id: int,
    folder_id: Optional[int],
    cog_ids: Optional[List[int]],
):
    """Replicate the legacy zip-download container's two API calls
    (``POST /cogs/service/cogs/files``, ``GET /cogs/service/folder/
    {id}/files``) as direct DB queries.

    Returns ``(files_info, folder_name)`` where files_info is the
    list of dicts the zip builder iterates and folder_name is what
    seeds the output filename.
    """
    import models

    if cog_ids:
        # Multi-cog selection — folder name is the parent of the
        # first cog if they all share one, else "download".
        files, folder_name = _files_for_cog_ids(db, tenant_id, cog_ids)
        return files, folder_name

    if folder_id is None:
        return [], "download"

    folder = db.query(models.Cog).filter(
        models.Cog.id == folder_id,
        models.Cog.tenant_id == tenant_id,
    ).first()
    folder_name = folder.name if folder else "download"
    files = _files_for_folder(db, tenant_id, folder_id)
    return files, folder_name


def _files_for_folder(
    db: Session, tenant_id: int, folder_id: int,
) -> List[Dict[str, Any]]:
    """Walk the cog tree under ``folder_id`` and collect every FILE
    cog's latest version's minio_path + relative path (preserving
    folder structure)."""
    import models

    out: List[Dict[str, Any]] = []

    def _walk(parent_id: int, rel_prefix: str):
        children = db.query(models.Cog).filter(
            models.Cog.parent_id == parent_id,
            models.Cog.tenant_id == tenant_id,
            models.Cog.deleted_at.is_(None),
        ).all()
        for c in children:
            if c.cog_type in (models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER):
                _walk(c.id, f"{rel_prefix}{c.name}/")
            elif c.cog_type == models.CogType.FILE:
                if not c.latest_version_id:
                    continue
                version = db.query(models.Version).filter(
                    models.Version.id == c.latest_version_id,
                ).first()
                if not version:
                    continue
                blob = db.query(models.ContentBlob).filter(
                    models.ContentBlob.id == version.content_id,
                ).first()
                if not blob or not blob.storage_path:
                    continue
                # storage_path is "minio://bucket/key" — strip the
                # scheme + bucket and keep just the key.
                key = blob.storage_path
                if "://" in key:
                    key = key.split("://", 1)[1]
                if "/" in key:
                    key = key.split("/", 1)[1]
                out.append({
                    "minio_path": key,
                    "relative_path": f"{rel_prefix}{c.name}",
                    "name": c.name,
                    # GY.V1c: label + mime so the zip builder can watermark
                    # labelled image members in place.
                    "data_classification": getattr(c, "data_classification", None),
                    "mime_type": blob.mime_type,
                })

    _walk(folder_id, "")
    return out


def _files_for_cog_ids(
    db: Session, tenant_id: int, cog_ids: List[int],
):
    """Materialise files for a multi-cog selection. If the cogs are
    all in the same parent folder, use that name; otherwise use
    'download'."""
    import models

    files: List[Dict[str, Any]] = []
    parents: set = set()
    for cog_id in cog_ids:
        c = db.query(models.Cog).filter(
            models.Cog.id == cog_id,
            models.Cog.tenant_id == tenant_id,
        ).first()
        if not c:
            continue
        if c.parent_id is not None:
            parents.add(c.parent_id)
        if c.cog_type in (models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER):
            sub = _files_for_folder(db, tenant_id, c.id)
            for s in sub:
                s["relative_path"] = f"{c.name}/{s['relative_path']}"
            files.extend(sub)
        elif c.cog_type == models.CogType.FILE:
            if not c.latest_version_id:
                continue
            version = db.query(models.Version).filter(
                models.Version.id == c.latest_version_id,
            ).first()
            if not version:
                continue
            blob = db.query(models.ContentBlob).filter(
                models.ContentBlob.id == version.content_id,
            ).first()
            if not blob or not blob.storage_path:
                continue
            key = blob.storage_path
            if "://" in key:
                key = key.split("://", 1)[1]
            if "/" in key:
                key = key.split("/", 1)[1]
            files.append({
                "minio_path": key,
                "relative_path": c.name,
                "name": c.name,
                # GY.V1c: label + mime so the zip builder can watermark
                # labelled image members in place.
                "data_classification": getattr(c, "data_classification", None),
                "mime_type": blob.mime_type,
            })

    folder_name = "download"
    if len(parents) == 1:
        parent_id = next(iter(parents))
        parent = db.query(models.Cog).filter(
            models.Cog.id == parent_id,
            models.Cog.tenant_id == tenant_id,
        ).first()
        if parent and parent.name:
            folder_name = parent.name
    return files, folder_name
