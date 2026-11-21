"""GX.R4 (launch-completion-megaplan Group X) — ephemeral run inputs.

``POST /v1/runs/ephemeral-inputs`` (multipart) uploads a file (audio/text/any)
into the tenant's existing content-addressed store and returns a short-TTL
reference id a run can hand an input node.

The upload materializes the bytes as a ``ContentBlob`` plus a HIDDEN, root
Cog + ``Version`` (so a run's input node consumes it through the unchanged
cog pipeline — ``COG_IDS`` → ``resolve-cog-hash`` → content-hash staging — with
no worker/container change) and records one ``AppEphemeralInput`` row.  30.6.12
c12a moved that body into ``services.ephemeral_run_inputs.upload_one()`` and
made the route dual-mode:

  * ONE ``file`` part  -> today's single shape + 201, byte-identical (B2).
  * >=2 ``file`` parts -> 200 ``{"items":[{index, ephemeral_input_id, cog_id,
    expires_at, content_hash, reused}, ...]}`` in input order; a part that
    fails carries ``{"index": i, "error": {"code", "detail"}}`` and the others
    still succeed.  Pre-write caps (count / summed bytes / per-file) reject the
    WHOLE request with 413 and write nothing.

  * Hidden from the library: the backing Cog is ``is_hidden=True`` at the
    tenant root (parent_id NULL), and the library listing filters
    ``is_hidden`` out (see crud.assets_cogs_files + assets_cogs_files_service).
  * Same filename twice -> NO 409: nothing is keyed by (name, parent) and
    content-addressed blobs dedup by hash, so a re-upload of an identical file
    is a cheap no-op and a different file of the same name just adds a row.
  * Auth: ANY authenticated caller (Bearer OR API key) via
    ``require_auditor_data_content_principal`` (a GY.C16 data-content guard
    wrapping ``require_bearer_or_api_key``) — this works for a run on behalf
    of any user (the App object, GX.A2, is not required) and applies the
    auditor data-content gate to API-key principals without forcing an
    Authorization header. Tenant-scoped: the tenant is ALWAYS
    ``current_user.tenant_id`` (the verified credential), never the body.
  * Size: enforces the tenant storage quota (the existing byte-ingress gate)
    and the per-file cap of the upload_size_cap "cog" category
    (middleware/upload_size_cap.py:105-106).

TTL 24h -> 7d grace -> bytes purged (all modes, owner decision OQ-5(a)); the
record row is never deleted. Expiry runs as the ``ephemeral_input_retention``
scheduler job (services.scheduler) via services.ephemeral_run_inputs.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile, status)
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

import models
from pydantic import BaseModel
from dependencies import get_db, require_auditor_data_content_principal
from services import ephemeral_run_inputs as eri
from utils.minio_client import MinioClient, get_tenant_bucket_name
from utils.request_principal import get_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["Ephemeral Run Inputs"])


def _caller_app_id() -> Optional[int]:
    """30.6.12 c12b — the App this request belongs to (row stamp + reuse key).

    ``utils.request_principal.get_api_key()`` is the contextvar set by
    ``require_bearer_or_api_key`` (dependencies.py:1336) on an API-key request;
    an App ApiKey row carries ``.app_id`` (dependencies.py:1327 reads the same
    attribute to scope the key, then :1335 calls
    ``request_principal.set_app_id``). Bearer (human) callers leave it unset, so
    the stamp is NULL and reuse is scoped by ``owner_user_id`` instead.

    No stack-identity source exists: ``utils/request_principal.py`` exposes no
    stack claim getter yielding an ``app_id`` (only the API-key contextvar), so
    a stack-identity caller stamps NULL in c12b — we never parse tokens here.
    """
    return getattr(get_api_key(), "app_id", None)


class EphemeralInputResponse(BaseModel):
    """Response shape for POST /runs/ephemeral-inputs (response_model)."""
    ephemeral_input_id: int
    cog_id: int
    name: str
    content_hash: str
    size_bytes: int
    expires_at: Optional[str] = None


class BatchEphemeralInputItem(BaseModel):
    """One accepted item of a batch upload (30.6.12 c12a)."""
    index: int
    ephemeral_input_id: int
    cog_id: int
    expires_at: Optional[str] = None
    content_hash: str
    reused: bool = False


class BatchEphemeralInputError(BaseModel):
    """One rejected item of a batch upload (others still succeed)."""
    index: int
    error: dict


class BatchEphemeralInputResponse(BaseModel):
    """Response shape for a multi-part POST /runs/ephemeral-inputs."""
    items: list[BatchEphemeralInputItem | BatchEphemeralInputError]


def _batch_error(index: int, code: str, detail: str) -> dict:
    return {"index": index, "error": {"code": code, "detail": detail}}


@router.post("/ephemeral-inputs", status_code=status.HTTP_201_CREATED,
             response_model=None,
             responses={
                 200: {"description":
                            "Batch mode (>=2 `file` parts): {\"items\": [{index, "
                            "ephemeral_input_id, cog_id, expires_at, content_hash, "
                            "reused} | {index, error: {code, detail}}]} in input "
                            "order."},
                 201: {"description":
                           "Single-file mode: {ephemeral_input_id, cog_id, name, "
                           "content_hash, size_bytes, expires_at} (unchanged)."},
                 413: {"description":
                           "Pre-write cap breach rejects the WHOLE request and "
                           "writes nothing; detail is one of batch_too_many_files "
                           "/ batch_too_large / file_too_large."},
             })
async def create_ephemeral_input(
    file: List[UploadFile] = File(...),
    name: Optional[str] = None,
    db: Session = Depends(get_db),
    # GY.C16 — data-content guard on the bearer/API-key principal (no
    # Authorization header forced, so API-key embed/app clients keep working).
    current_user: models.User = Depends(require_auditor_data_content_principal),
):
    """Upload one short-TTL input file, or several in one request.

    Multipart: ``file`` (repeatable) plus optional ``name`` (single-file mode
    only; batch mode uses each part's own filename). One part -> today's
    201 shape unchanged. Two or more parts -> 200 ``{"items": [...]}`` in
    input order, per-item errors reported as ``error: {code, detail}``.
    """
    uploads = list(file or [])
    if not uploads:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="A file (or a name) is required.")
    if len(uploads) == 1 and not uploads[0].filename and not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="A file (or a name) is required.")

    if len(uploads) == 1:
        # Single-file mode: EXACTLY today's status (201) + shape.
        try:
            return eri.upload_one(db, tenant_id=current_user.tenant_id,
                                  user=current_user, upload=uploads[0],
                                  name=name,
                                  bucket=get_tenant_bucket_name(
                                      current_user.tenant_id, db),
                                  client_factory=MinioClient,
                                  app_id=_caller_app_id())
        except eri.EphemeralFileTooLargeError as e:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                detail="file_too_large") from e
        except eri.EphemeralUploadFailedError as e:
            logger.error("ephemeral upload to storage failed: %s", e.message)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail="Failed to store the file.") from e

    # ---- Batch mode (>=2 `file` parts) ------------------------------------
    # S2: every cap is checked BEFORE any DB write or storage put.
    max_files = eri.batch_max_files(db)
    if len(uploads) > max_files:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="batch_too_many_files")

    # Pre-write caps (S2/S5): sizes are measured by streaming each part and
    # DISCARDING it (services.ephemeral_run_inputs.measure_upload) — a
    # multipart part has no trustworthy declared length, and the read
    # hard-stops at the cap, so nothing unbounded is ever buffered.
    cap = eri.per_file_cap_bytes(db, current_user.tenant_id)
    total_bytes = 0
    try:
        for part in uploads:
            total_bytes += eri.measure_upload(part, cap)
            if total_bytes > eri.batch_max_total_bytes(db):
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                    detail="batch_too_large")
    except eri.EphemeralFileTooLargeError as e:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="file_too_large") from e

    items: list = []
    for index, upload in enumerate(uploads):
        try:
            body = eri.upload_one(db, tenant_id=current_user.tenant_id,
                                  user=current_user, upload=upload, name=None,
                                  bucket=get_tenant_bucket_name(
                                      current_user.tenant_id, db),
                                  client_factory=MinioClient,
                                  app_id=_caller_app_id())
        except eri.EphemeralFileTooLargeError as e:
            items.append(_batch_error(index, "file_too_large",
                                      "File exceeds the per-file upload cap."))
            continue
        except eri.EphemeralUploadFailedError as e:
            items.append(_batch_error(index, e.code, e.message))
            continue
        items.append({
            "index": index,
            "ephemeral_input_id": body["ephemeral_input_id"],
            "cog_id": body["cog_id"],
            "expires_at": body["expires_at"],
            "content_hash": body["content_hash"],
            "reused": False,  # c12b adds reuse; c12a never reports it
        })

    return JSONResponse(status_code=status.HTTP_200_OK, content={"items": items})


@router.get("/ephemeral-inputs", response_model=None,
            responses={
                200: {"description":
                          "{\"items\": [{ephemeral_input_id, cog_id, name, "
                          "content_hash, size_bytes, expires_at}, ...]} — every "
                          "live row THIS principal may see for that hash (newest "
                          "first, max 50). A hash owned by another tenant/App/user "
                          "is indistinguishable from an unknown hash: 200 with an "
                          "empty list, never 404/403 (no existence oracle)."},
                422: {"description":
                          "content_hash missing (FastAPI's own 422) or not "
                          "lowercase sha256 hex, exactly 64 chars "
                          "(detail.code = invalid_content_hash). Case is never "
                          "folded: uppercase is invalid, so the endpoint never "
                          "answers \"that hash exists, you typed it wrong\"."},
            })
async def lookup_ephemeral_input_by_hash(
    content_hash: str = Query(...),
    db: Session = Depends(get_db),
    # S4: the SAME auth dependency as the POST — never a weaker one.
    current_user: models.User = Depends(require_auditor_data_content_principal),
):
    """Look up this principal's live ephemeral inputs by content hash (30.6.12 c12c).

    Read-only: no TTL refresh, no audit row (a GET is not a reuse). Scope is the
    one filter c12b's reuse query uses (tenant + app_id, plus owner_user_id when
    app_id is NULL), so the answer never reveals whether the bytes exist under
    another principal.
    """
    if not eri.CONTENT_HASH_RE.match(content_hash):
        # The router's only hand-written error shape is {code, detail}
        # (routers/ephemeral_run_inputs.py:_batch_error); reuse it for 422.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"code": "invalid_content_hash",
                                    "detail": "content_hash must be 64-char "
                                              "lowercase sha256 hex."})
    rows = eri.query_by_hash(db, tenant_id=current_user.tenant_id,
                             app_id=_caller_app_id(),
                             user_id=getattr(current_user, "id", None),
                             content_hash=content_hash)
    return {"items": [eri._response_for(row) for row in rows]}
