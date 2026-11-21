"""Per-tenant Named Artifact registry (named-artifact-registry).

A small, tenant-scoped registry of reusable pipeline artifacts so workflow nodes
can refer to a produced artifact BY NAME rather than by an opaque collection id /
filename the user must copy around:

  * type == "rag"  — a built RAG corpus. ``ref`` is the vector-store collection /
    namespace the RAG Query node reads back.
  * type == "lora" — a trained SDXL character LoRA. ``ref`` is the resolvable
    location of the ``.safetensors`` (the filename the brand-sdxl server finds
    under its mounted ``/loras`` dir → resolve-by-name for Generate).

Endpoints (tenant-scoped via the caller's credential — NEVER a body field):
  * ``POST /named-artifacts``           — upsert (register/update) by
    (tenant, type, name). Called by the producing nodes' run tokens.
  * ``GET  /named-artifacts?type=rag``  — list the caller's tenant's artifacts.
    Response shape is fixed for the WB picker/dropdown:
    ``[{name, type, ref, description, metadata, updated_at}]``.

Auth — this endpoint is reachable by the SAME node credentials the RAG / SDXL /
LoRA nodes already carry. Because ``mod_node_auth`` prefers the worker-vended
run token (MOD_TASK_TOKEN_URL) when present, a node may present EITHER:
  * a ``vector`` run token (RAG nodes' injected API_KEY), OR
  * a ``run_task`` token (general nodes: SDXL Generate, LoRA Train), OR
  * a build-task token / platform master key / ordinary user bearer (Dashboard,
    tim's own calls, tests).
All resolve the tenant from the verified credential, so cross-tenant access is
impossible by construction.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_user_or_build_token
from services import named_artifacts_service as _service
from services._base import ServiceError
from utils.build_task_token import (
    verify_run_task_token,
    verify_vector_run_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/named-artifacts", tags=["named-artifacts"])


def require_named_artifact_tenant(
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> int:
    """Resolve the tenant for a named-artifact op from the verified credential —
    NEVER from the request body. Accepts, in order:
      1. a run-scoped vector token (RAG node),
      2. a run_task node token (general nodes: SDXL Generate, LoRA Train),
      3. a build-task token / master key / ordinary user bearer.
    Missing/invalid → 401/403 before any DB write."""
    if x_api_key:
        vclaims = verify_vector_run_token(x_api_key)
        if vclaims and vclaims.get("tenant_id") is not None:
            return int(vclaims["tenant_id"])
        rclaims = verify_run_task_token(x_api_key)
        if rclaims and rclaims.get("tenant_id") is not None:
            return int(rclaims["tenant_id"])
    ctx = require_user_or_build_token(
        authorization=authorization, x_api_key=x_api_key, db=db,
    )
    if ctx.tenant_id is not None:
        return int(ctx.tenant_id)
    if ctx.user is not None and getattr(ctx.user, "tenant_id", None) is not None:
        return int(ctx.user.tenant_id)
    raise HTTPException(status_code=403, detail="Credential carries no tenant scope.")


def named_artifact_actor(
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Optional[models.User]:
    """FX.148 p4a part 2 — the USER behind a registration, when there is one.

    `require_named_artifact_tenant` deliberately accepts node credentials
    (vector/run-task tokens) that carry a tenant but NO user, which is exactly
    how the LoRA Export node registers a trained LoRA. So the actor is
    optional: resolve it best-effort here and pass it to the provenance
    recorder. Fail-soft by construction — any credential that the tenant
    dependency already accepted may still fail THIS re-resolution (build token,
    master key), and that must not 403 the registration; the tenant scope is
    the enforced one, this is attribution only."""
    try:
        ctx = require_user_or_build_token(
            authorization=authorization, x_api_key=x_api_key, db=db,
        )
    except Exception:  # noqa: BLE001 — attribution only; tenant scope is enforced above
        return None
    return ctx.user


class _UpsertRequest(BaseModel):
    type: str = Field(..., description='Artifact type: "rag" or "lora".')
    name: str = Field(..., min_length=1, max_length=200)
    ref: str = Field(..., min_length=1, description=(
        "rag: collection/namespace; lora: resolvable location (filename under "
        "the SDXL server's /loras, or an S3 key + fetch step)."
    ))
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class _ArtifactOut(BaseModel):
    name: str
    type: str
    ref: str
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    # FX.131 (Q118=A) — training provenance (dataset ids + likeness-consent
    # link) on trained-model artifacts; None for rag artifacts / unset.
    training_record: Optional[Dict[str, Any]] = None
    updated_at: Optional[datetime] = None


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer error to HTTP. bad_request -> 400 (matches this
    domain's prior inline-validation behaviour)."""
    status = {"bad_request": 400, "not_found": 404, "conflict": 409,
              "forbidden": 403}.get(exc.code, 400)
    raise HTTPException(status_code=status, detail=exc.message)


@router.post("", response_model=_ArtifactOut)
@router.post("/", response_model=_ArtifactOut, include_in_schema=False)
def upsert_named_artifact(
    request: _UpsertRequest,
    tenant_id: int = Depends(require_named_artifact_tenant),
    actor: Optional[models.User] = Depends(named_artifact_actor),
    db: Session = Depends(get_db),
):
    """Register or update a named artifact for the caller's tenant. Upsert key is
    (tenant_id, type, name) among live rows — a second call with the same name
    updates ref/description/metadata in place rather than creating a duplicate."""
    try:
        row = _service.upsert_named_artifact(
            db,
            tenant_id=tenant_id,
            art_type=request.type,
            name=request.name,
            ref=request.ref,
            description=request.description,
            metadata=request.metadata,
            user=actor,
        )
    except ServiceError as exc:
        _raise_http(exc)
    return _artifact_out(row)


@router.get("", response_model=List[_ArtifactOut])
@router.get("/", response_model=List[_ArtifactOut], include_in_schema=False)
def list_named_artifacts(
    type: Optional[str] = Query(None, description='Filter by type: "rag" or "lora".'),
    tenant_id: int = Depends(require_named_artifact_tenant),
    db: Session = Depends(get_db),
):
    """List the caller's tenant's live named artifacts (optionally filtered by
    type). Response shape is fixed for the WB dropdown:
    ``[{name, type, ref, description, metadata, updated_at}]``."""
    try:
        rows = _service.list_named_artifacts(db, tenant_id=tenant_id, art_type=type)
    except ServiceError as exc:
        _raise_http(exc)
    return [_artifact_out(r) for r in rows]


def _artifact_out(row) -> _ArtifactOut:
    return _ArtifactOut(
        name=row.name, type=row.type, ref=row.ref,
        description=row.description, metadata=row.artifact_metadata,
        training_record=row.training_record,
        updated_at=row.updated_at,
    )


class _PublishOut(BaseModel):
    name: str
    type: str
    ref: str
    publishable: bool
    consent_record_id: Optional[int] = None
    consent_type: Optional[str] = None
    dataset_ids: Optional[List[int]] = None


class _DownloadOut(BaseModel):
    name: str
    type: str
    ref: str
    object_key: str
    download_url: str
    training_record: Optional[Dict[str, Any]] = None
    # GSEC FX.79 p3b — the export stamp for a data-marked artifact. The
    # .safetensors bytes are never touched (the presigned object cannot carry
    # response headers), so the marking rides in the hand-off body: the label
    # (with its dissemination addendum) and the same banner line the UIs and
    # the X-MOD-Marking header show. Absent (null) for an unmarked artifact.
    mod_marking: Optional[str] = None
    mod_marking_banner: Optional[str] = None


@router.get("/{artifact_id}", response_model=_ArtifactOut)
def get_named_artifact(
    artifact_id: int,
    tenant_id: int = Depends(require_named_artifact_tenant),
    db: Session = Depends(get_db),
):
    """Fetch one of the caller's tenant's named artifacts by id. Cross-tenant
    or missing ids return 404 (a foreign id looks like a missing one — the
    tenant scope is enforced in the service, never revealed as 403)."""
    try:
        row = _service.get_named_artifact(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
    except ServiceError as exc:
        _raise_http(exc)
    return _artifact_out(row)


@router.get("/{artifact_id}/download", response_model=_DownloadOut)
def download_trained_model(
    artifact_id: int,
    tenant_id: int = Depends(require_named_artifact_tenant),
    db: Session = Depends(get_db),
):
    """Download a trained-model (lora) artifact for the caller's tenant.

    FX.131 (Q118=A): customer-owned models. The .safetensors is resolved from
    the CALLER's own tenant bucket only (never a foreign bucket or shared
    path) and a short-lived presigned GET is returned. Every hand-off writes a
    TRAINED_MODEL_DOWNLOADED audit row. Non-lora artifacts and foreign/missing
    ids are rejected (400 / 404). Node/run-token callers attribute the audit
    row to the tenant's first user — the tenant id in the row is the
    authoritative scope. We pass actor_user_id=None; the service resolves a
    real user (the tenant's first) because audit actor_user_id is a NOT NULL
    FK to users.id."""
    try:
        result = _service.download_trained_model(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
            minio_client=_build_minio(tenant_id), actor_user_id=None,
        )
    except ServiceError as exc:
        _raise_http(exc)
    # GSEC FX.79 p3b — a data-marked artifact (CUI / ITAR / export-controlled
    # admin mark on this named artifact) stamps its download: the marking
    # rides in the hand-off body (a presigned object cannot carry response
    # headers) AND as X-MOD-Marking on this JSON response. The .safetensors
    # bytes are never rewritten.
    try:
        from services import marking_stamps as _mk
        stamp = _mk.export_stamp(db, tenant_id, "named_artifact", artifact_id)
        if stamp:
            result = {**result,
                      "mod_marking": stamp["header"],
                      "mod_marking_banner": stamp["banner"]}
            from fastapi.responses import JSONResponse
            return JSONResponse(
                content=_DownloadOut(**result).model_dump(mode="json"),
                headers=_mk.stamp_headers(stamp),
            )
    except Exception:  # noqa: BLE001 — a stamp must never break a download
        logger.warning("named-artifact download stamp skipped for %s", artifact_id)
    return _DownloadOut(**result)


@router.post("/{artifact_id}/publish", response_model=_PublishOut)
def publish_named_artifact(
    artifact_id: int,
    tenant_id: int = Depends(require_named_artifact_tenant),
    db: Session = Depends(get_db),
):
    """FX.131.s1 (Q118=A) — the explicit marketplace-publish action for a
    trained model. Default is NOT publishable: the request is refused (403)
    unless the artifact's training record carries the explicit
    ``marketplace_publish`` flag AND a likeness-consent link id
    (``consent_record_id``). The flag lives in the existing training_record
    JSONB — no new column, so an un-flagged row is never publishable.

    This is the gate only: it records/validates the customer's publish
    decision. Handing the .safetensors to the Marketplace listing store is a
    separate, later step (the Marketplace app owns that store)."""
    try:
        result = _service.publish_named_artifact(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
    except ServiceError as exc:
        _raise_http(exc)
    return _PublishOut(**result)


def _build_minio(tenant_id: int):
    """Tenant-scoped MinioClient for the caller's bucket (storage
    encryption at rest is whatever the tenant's existing storage
    configuration provides — this endpoint never bypasses it)."""
    from utils.minio_client import MinioClient
    return MinioClient(tenant_id=tenant_id)
