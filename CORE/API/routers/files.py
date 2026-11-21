from fastapi import APIRouter, Depends, UploadFile, File as FastAPIFile, HTTPException, status, Form, Request
from sqlalchemy.orm import Session
from typing import Optional
from io import BytesIO
import hashlib
import schemas, crud, models
from dependencies import (get_db, require_active_and_onboarded_user,
                          get_tenant_minio_client, require_auditor_data_content)
from utils.tenant_lifecycle import require_tenant_writable
from utils.minio_client import MinioClient
from services import files_service
try:
    from services import malware_scan_async
except ImportError:
    # CE: no async malware-scan backend ships — guard, not reclassify.
    malware_scan_async = None
from services._base import NotFoundError
import uuid
import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Files"],
    dependencies=[
        Depends(require_active_and_onboarded_user),
        # GY.C16: auditor accounts have no data content unless their
        # grant explicitly sets data_content_granted.
        Depends(require_auditor_data_content),
        # GSEC FX.59 part 2 (Q42=A): contract-end lifecycle gate — 423
        # while the tenant is read_only or suspended (platform admins
        # bypass). The router holds only the upload write route.
        Depends(require_tenant_writable),
    ],
)


def _stream_object(response):
    """Yield bytes from a storage get_object response (urllib3 HTTPResponse:
    .stream/.close/.release_conn; it has no iter_chunks)."""
    try:
        for chunk in response.stream(8192):
            if chunk:
                yield chunk
    finally:
        try:
            response.close()
            response.release_conn()
        except Exception:
            pass


@router.post("/upload", response_model=schemas.File)
async def upload_file(
        file: UploadFile = FastAPIFile(...),
        cog_id: Optional[int] = Form(None),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
        minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Uploads a file by creating both a Cog record (for hierarchy/permissions)
    and a File record (for storage), then uploading to MinIO.

    Process:
    1. Create Cog record with cog_type=FILE
    2. Create File record linked to the Cog via file.cog_id
    3. Upload to MinIO using File.id as object name
    4. Update File record with storage_path

    If cog_id is provided, it's used as the parent folder for the new Cog.
    Permission checks are performed on the parent folder.
    """
    # Check folder permission if cog_id is provided
    if cog_id is not None:
        try:
            files_service.authorize_upload_folder(db, cog_id=cog_id, user=current_user)
        except NotFoundError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=e.message
            )
        except files_service.ForbiddenError as e:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=e.message
            )

    # ========================================================================
    # STORAGE LIMIT CHECK - Ensure tenant has available storage
    # ========================================================================
    files_service.enforce_storage_limit(db, user=current_user, file_size_bytes=file.size or 0)

    # S14: Enforce file size limit (500MB max via this legacy endpoint)
    MAX_UPLOAD_SIZE = files_service.MAX_UPLOAD_SIZE  # 500MB
    file_content = await file.read()
    if len(file_content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)}MB. Use multipart upload for larger files."
        )

    # FX.90 part 1 (Q76=A): scan BEFORE storing — and before any Cog/File
    # record is created, because the security-audit write commits. No-op
    # while malware_scan.enabled is False (the default): behaviour unchanged.
    # FX.90 p3a (Q76=A): uploads at/above `malware_scan.async_above_bytes`
    # skip this inline scan — large media is scanned ASYNC (the artefact is
    # stored scan-pending below and queued to services/malware_scan_async.py).
    # Small files take this branch exactly as before, byte-for-byte.
    # CE guard: no async malware-scan backend ships, so large uploads never
    # take the async-queue branch below — they fall through to the inline
    # gate_upload_malware_scan() call untouched (a real CE capability gap,
    # not a bug).
    _fx90_async = (
        malware_scan_async.is_async_size(len(file_content))
        if malware_scan_async is not None else False
    )
    if not _fx90_async:
        try:
            files_service.gate_upload_malware_scan(
                db, user=current_user, filename=file.filename, content=file_content
            )
        except files_service.MalwareInfectedError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.message
            )
        except files_service.MalwareScanUnavailableError as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=e.message
            )

    # 1. First create a Cog record for this file (for hierarchy/permissions)
    db_cog = models.Cog(
        tenant_id=current_user.tenant_id,
        owner_id=current_user.id,
        parent_id=cog_id,  # Link to parent folder if provided
        name=file.filename,
        cog_type=models.CogType.FILE,  # Mark as file type
        processing_metadata={},
        total_processing_cost=0,
        processing_count=0
    )
    db.add(db_cog)
    db.flush()  # Get the Cog ID without committing

    # FX.90 p3a (Q76=A): a large upload skipped the inline scan above, so
    # mark it scan-pending + queue the async streaming scan IN THIS
    # transaction (the commit at the end finalizes cog + file + marker +
    # queue row together — no half-scanned artefact is ever visible).
    if _fx90_async:
        malware_scan_async.mark_and_queue(
            db, cog_id=db_cog.id, tenant_id=current_user.tenant_id,
            user_id=current_user.id, size_bytes=len(file_content))

    # 2. Compute SHA256 hash for content-addressed storage
    content_hash = hashlib.sha256(file_content).hexdigest()

    # Extract file extension (lowercase, no dot) for database storage
    file_extension = ""
    if file.filename and '.' in file.filename:
        file_extension = file.filename.rsplit('.', 1)[-1].lower()

    # FLAT STORAGE: Files stored as pure SHA256 hash (no extension) in MinIO
    # Extension stored in database for processing tools and downloads
    object_name = content_hash  # Pure hash, no extension

    # Create the File record linked to the Cog we just created
    file_data_for_db = schemas.FileCreate(
        name=file.filename,
        size_bytes=file.size,
        mime_type=file.content_type,
        storage_path="",  # This is a temporary placeholder
        cog_id=db_cog.id  # Link to the file's own Cog, not parent folder
    )
    db_file = crud.create_file_record(db, file_data=file_data_for_db, tenant_id=current_user.tenant_id,
                                      user_id=current_user.id)

    try:
        # 3. Upload the file to Minio using pure hash as object name
        file_stream = BytesIO(file_content)
        storage_path = await minio_client.upload_file(
            object_name=object_name,
            file_stream=file_stream,
            file_size=len(file_content),
            content_type=file.content_type
        )
    except Exception as e:
        # If Minio upload fails, roll back BOTH the File and Cog records.
        # ISO 27001 no-hard-delete (MEMORY iso-no-hard-delete): File and Cog
        # both carry SoftDeleteMixin, so stamp `deleted_at` and RETAIN the row
        # instead of hard-removing it. Their reads already filter tombstones
        # (crud soft-delete filters / cog soft-delete recovery path).
        from datetime import datetime as _dt, timezone as _tz
        _now = _dt.now(_tz.utc)
        db_file.deleted_at = _now
        db_cog.deleted_at = _now
        db.commit()
        logger.error("Failed to upload file to storage: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload file to storage"
        )

    # 4. Update the database record with the final, correct storage_path
    db_file.storage_path = storage_path

    # 5. Create or update ContentBlob record for deduplication and extension tracking
    # (same content re-uploaded - even after the previous file was deleted -
    # keys to the same hash, so this is a create-or-update, not a plain insert)
    files_service.upsert_content_blob(
        db,
        tenant_id=current_user.tenant_id,
        content_hash=content_hash,
        storage_path=storage_path,
        mime_type=file.content_type,
        file_extension=file_extension,
        cog_id=db_cog.id,
        size_bytes=len(file_content)
    )

    # 6. Stamp a Version on the Cog so that run input validation (which requires
    # a versioned Cog) accepts it — same shape as ephemeral_run_inputs.py:171.
    from datetime import datetime, timezone

    version = models.Version(
        cog_id=db_cog.id,
        tenant_id=current_user.tenant_id,
        content_id=content_hash,
        version_number=1,
        name=file.filename,
        author_user_id=current_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(version)
    db.flush()
    db_cog.latest_version_id = version.id

    db.commit()
    db.refresh(db_file)
    db.refresh(db_cog)

    # 6. Return the file object
    return db_file

# consign 0.7e (unblock): the one POST /v1/files/upload returns is a File.id
# (UUID) with no way to read the bytes back. This route serves them, tenant-
# scoped: another tenant's id is a 404 (never 403), so existence never leaks.
# 739 (WSIP fix-verification + run panels): outputs arrive as content hashes
# (`content:<hash>`) and no route served bytes by hash, so images rendered as
# text chips. Tenant-scoped streaming: another tenant's hash (or an unknown
# one) is a plain 404 - never a 403 - so existence never leaks.
@router.get("/content-by-hash/{content_hash}", response_model=None)
async def get_file_content_by_hash(
        content_hash: str,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
        minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Streams the bytes of a tenant ContentBlob by its content hash.

    Tenant-scoped lookup (crud.files.get_content_blob): a hash that belongs
    to another tenant, or that is unknown, is a 404 "Content not found" -
    never a 403 - so the route never confirms existence outside the caller's
    own tenant. Bytes come from the same tenant-scoped storage client the
    upload route writes through, keyed by ContentBlob.storage_path (the
    content-addressed object name; the bare hash when the column is unset,
    as services/file_processing.py stats it). Never a presigned URL.
    """
    from fastapi.responses import StreamingResponse
    import os
    import re

    if not re.fullmatch(r"[0-9a-f]{32,128}", content_hash or ""):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="content_hash must be lowercase hex, 32-128 chars"
        )

    from crud.files import get_content_blob

    blob = get_content_blob(
        db, content_hash=content_hash, tenant_id=current_user.tenant_id
    )
    if not blob:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Content not found"
        )

    sp = blob.storage_path or content_hash
    storage_scheme = os.getenv('STORAGE_SCHEME', 's3')
    if sp.startswith((f'{storage_scheme}://', 's3://', 'minio://')):
        path_after_scheme = sp.split('://', 1)[1]
        object_path = ('/'.join(path_after_scheme.split('/')[1:])
                       if '/' in path_after_scheme else path_after_scheme)
    else:
        object_path = sp

    try:
        response = minio_client.client.get_object(minio_client.bucket_name, object_path)
    except Exception as e:
        logger.error("Error streaming content by hash (%s): %s", content_hash, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error streaming file"
        )

    mime_type = blob.mime_type or "application/octet-stream"
    ext = (blob.file_extension or "").lower()
    safe_name = content_hash + (f".{ext}" if ext else "")
    headers = {"X-Content-Type-Options": "nosniff"}
    mime_lower = mime_type.lower()
    xss_risk = mime_lower in ("text/html", "image/svg+xml")
    disposition = "inline" if (not xss_risk and (mime_lower.startswith("image/")
                               or mime_lower.startswith("text/")
                               or mime_lower == "application/pdf")) else "attachment"
    headers["Content-Disposition"] = f'{disposition}; filename="{safe_name}"'
    if blob.size_bytes:
        headers["Content-Length"] = str(blob.size_bytes)

    return StreamingResponse(_stream_object(response), media_type=mime_type, headers=headers)


@router.get("/{file_id}/content", response_model=None)
async def get_file_content(
        file_id: str,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
        minio_client: MinioClient = Depends(get_tenant_minio_client)
):
    """
    Streams the bytes of an uploaded File record by its UUID id.

    Tenant-scoped lookup (crud.file_cud.get_file_record): an id that belongs
    to another tenant, or that is soft-deleted, is a 404 - never a 403 - so
    the route never confirms existence outside the caller's own tenant.
    Bytes come from the same tenant-scoped storage client the upload route
    writes through (get_tenant_minio_client -> utils/storage.py), keyed by
    File.storage_path (the content-addressed object name).
    """
    from fastapi.responses import StreamingResponse
    import os

    try:
        uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="file_id must be a UUID"
        )

    file_record = crud.get_file_record(
        db, file_id=file_id, tenant_id=current_user.tenant_id
    )
    if not file_record or not file_record.storage_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # CUIP.11d — the shelf "open" route hands the phone a download_url-less
    # verdict and the phone fetches bytes through THIS route, so a marked
    # asset could otherwise skip the mobile-CUI guard entirely. One call
    # before presign/stream: no-op for every non-mobile azp (web/desktop/
    # API-key behaviour unchanged) and for unmarked assets; a mobile-client
    # token downloading a marked asset needs valid device headers, an
    # ACTIVE shelf row and enough attestation tier — the same guard, and
    # its one CUIP.12 audit row, the shelf route already applies. The azp
    # is read from the SAME Authorization header the auth dependency above
    # already signature-validated, so the claim is trustworthy here.
    if file_record.cog_id is not None:
        # 1788b CE carve: mobile_cui_policy is paid; the mobile-CUI download
        # guard (device attestation + shelf rows) is a paid CUIP.11 feature, so
        # CE serves the download with no mobile-CUI enforcement.
        from utils.paid_import import optional_paid
        _mcp = optional_paid("services.mobile_cui_policy")
        if _mcp is not None:
            _enforce_mobile_download = _mcp.enforce_mobile_download
            _enforce_mobile_download(
                db,
                token_azp=_mcp.azp_from_request(http_request),
                device_id_header=http_request.headers.get("X-MOD-Device-Id"),
                device_token_header=http_request.headers.get("X-MOD-Device-Token"),
                tenant_id=current_user.tenant_id,
                user_id=current_user.id,
                resource_type="asset",
                resource_id=file_record.cog_id,
            )

    # Audit: same CONTENT_DOWNLOAD row the cog download route writes (keyed
    # on the file's own Cog, which every upload creates). Non-fatal.
    if file_record.cog_id is not None:
        try:
            from audit import log_permission_audit
            from models import AuditAction

            log_permission_audit(
                db=db, action=AuditAction.CONTENT_DOWNLOAD,
                actor_user_id=current_user.id, tenant_id=current_user.tenant_id,
                resource_type="cog", resource_id=file_record.cog_id,
                request=http_request,
                metadata={"file_name": file_record.name,
                          "file_id": file_record.id,
                          "delivery_path": "file_content"},
            )
        except Exception:
            pass

    # GT.D10 — meter + enforce the egress cap (402 at 100%).
    # egress_meter is Paid (absent in the CE carve) — skip silently there, but
    # ONLY absence: the decision goes through the CE paid-import guard
    # (utils/paid_import.py optional_paid), never a bare try/except ImportError,
    # so a real ImportError from a broken paid module propagates (fail closed).
    from utils.paid_import import optional_paid
    _egress_meter = optional_paid("services.egress_meter")
    if _egress_meter is not None:
        _egress_meter.check_and_record(
            db, current_user.tenant_id,
            file_record.size_bytes or 0,
            kind="file_content", via_mod_proxy=True)

    storage_scheme = os.getenv('STORAGE_SCHEME', 's3')
    sp = file_record.storage_path
    if sp.startswith((f'{storage_scheme}://', 's3://', 'minio://')):
        path_after_scheme = sp.split('://', 1)[1]
        object_path = ('/'.join(path_after_scheme.split('/')[1:])
                       if '/' in path_after_scheme else path_after_scheme)
    else:
        object_path = sp

    try:
        response = minio_client.client.get_object(minio_client.bucket_name, object_path)
    except Exception as e:
        logger.error("Error streaming file content (%s): %s", file_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error streaming file"
        )

    mime_type = file_record.mime_type or "application/octet-stream"
    safe_name = (file_record.name or "file").replace('"', "").replace("'", "")
    headers = {"Content-Disposition": f'inline; filename="{safe_name}"'}
    if file_record.size_bytes:
        headers["Content-Length"] = str(file_record.size_bytes)

    return StreamingResponse(_stream_object(response), media_type=mime_type, headers=headers)
