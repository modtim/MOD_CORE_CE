"""System → External: load MOD is putting on external orchestrators + the farm registry.

Tenant-scoped operational view of the executor-submit nodes' work (Kubernetes /
Qube / Deadline) — see services.external_load — PLUS the registry rows from
services/external_farms.py  so the page can actually ADD a farm
(owner 11-04: "External Orchestrators page has no way to add a farm").

  GET    /system/external              — per-orchestrator load (unchanged)
  GET    /system/external/farms        — this tenant's registered farms
  POST   /system/external/farms        — register one (tenant admin)
  DELETE /system/external/farms/{id}   — drop one (tenant admin)

Any active, onboarded user can VIEW their own tenant's data; the WRITE routes
add the tenant-admin gate (same shape as routers/admin_scim_tokens.py). Every
service call is passed ``current_user.tenant_id`` — a foreign tenant's farm id
reads as not-found, exactly like a missing id.

NO "Test Connection" endpoint and NO outbound request of any kind: an outbound
probe would need the SSRF/egress-allowlist review (GSEC FX.151 / utils/safe_http.py)
and is deliberately deferred. The endpoint a farm registers is validated
syntactically only (urllib.parse.urlsplit) and never contacted.

SECRETS GO THROUGH THE VAULT: the credential arrives only in the POST JSON body,
is wrapped in SecretStr, handed to the service (which writes it to Vault KV v2
and keeps only the reference path on the row), and is never echoed in a response,
a header, a log line, audit metadata or an exception text.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, SecretStr
from sqlalchemy.orm import Session

import models
import audit
from dependencies import get_db, require_active_and_onboarded_user
from services.external_load import external_overview
from services.external_farms import (
    create_farm, delete_farm, list_farms,
    DuplicateFarmNameError, ExternalFarmVaultError,
)

logger = logging.getLogger(__name__)

class _ScrubbedValidationRoute(APIRoute):
    """Route class for THIS router only — never an app-wide handler.

    FastAPI validates the JSON body BEFORE the endpoint runs, and its default
    422 for a malformed body embeds `input` — the RAW submitted JSON. These
    routes are the only ones that accept a plaintext `credential` in that body,
    so the default answer would echo the caller's secret back to it (and into
    any access log of the response). FastAPI 0.115.14 raises RequestValidationError
    INSIDE the handler that ``get_route_handler()`` builds — the exception-handler
    stack that renders the default 422 lives OUTSIDE the route, so an ``handle()``
    override never sees the error. Wrap the route handler instead and answer 422
    with loc/msg/type only: no `input`, no `ctx`, for every error. Status and
    top-level shape otherwise unchanged. `msg` is safe to pass through: this file
    defines no custom pydantic validator whose message interpolates a submitted
    value (FarmCreateRequest is plain `str` / `SecretStr | None` fields, and every
    length/format check here is an HTTPException raised AFTER validation with a
    static message), so no pydantic message can carry the credential.
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def scrubbed(request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                errors = [
                    {"loc": list(err.get("loc", [])),
                     "msg": err.get("msg", ""),
                     "type": err.get("type", "")}
                    for err in exc.errors()
                ]
                return JSONResponse(status_code=422, content={"detail": errors})

        return scrubbed


router = APIRouter(prefix="/system", tags=["System - External Orchestrators"],
                   route_class=_ScrubbedValidationRoute)

FARM_KINDS = ("kubernetes", "qube", "deadline")
FARM_NAME_MAX = 120
FARM_ENDPOINT_MAX = 512
FARM_CREDENTIAL_MAX = 4096
FARM_SCHEMES = ("http", "https")


def _validate_farm_endpoint(endpoint: str) -> str:
    """Syntactic guard only — nothing here resolves or contacts the endpoint."""
    if len(endpoint) > FARM_ENDPOINT_MAX:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_farm_endpoint",
                    "message": "Farm endpoint is too long."})
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f for ch in endpoint):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_farm_endpoint",
                    "message": "Farm endpoint must not contain whitespace or control characters."})
    parts = urlsplit(endpoint)
    if parts.scheme.lower() not in FARM_SCHEMES:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_farm_endpoint",
                    "message": "Farm endpoint must use http or https."})
    host = parts.hostname
    if not host or "%" in host or not any(ch.isalnum() for ch in host):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_farm_endpoint",
                    "message": "Farm endpoint must name a host."})
    if "@" in parts.netloc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_farm_endpoint",
                    "message": "Farm endpoint must not carry a user or password."})
    if parts.fragment:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_farm_endpoint",
                    "message": "Farm endpoint must not carry a fragment."})
    return endpoint


def _require_tenant_admin(user: models.User) -> None:
    """Farm registry writes are tenant-admin only — mirrors the SCIM surface."""
    if getattr(user, "is_platform_admin", False):
        return
    if user.parent_user_id is None:
        return
    raise HTTPException(
        status_code=403,
        detail={"code": "tenant_admin_required",
                "message": "Only tenant admins can manage external farms."})


def _safe_audit(db, user, action, resource_id, metadata):
    try:
        audit.log_permission_audit(
            db=db, action=action,
            actor_user_id=user.id, tenant_id=user.tenant_id,
            resource_type="external_farm", resource_id=resource_id,
            metadata=metadata,
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("External farm audit emit failed for action=%s.", action)


class FarmCreateRequest(BaseModel):
    kind: str
    name: str
    endpoint: str
    # SecretStr so nothing downstream can print it; accepted ONLY in the POST body.
    credential: SecretStr | None = None


class FarmSummary(BaseModel):
    id: int
    kind: str
    name: str
    endpoint: str
    is_active: bool
    has_credential: bool
    created_at: datetime | None = None


class FarmDeleteResponse(BaseModel):
    id: int
    deleted: bool


@router.get("/external")
def get_external_overview(
    window_hours: int = 24,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Per-orchestrator, per-farm load MOD has dispatched for this tenant:
    active / queued / succeeded / failed, durations, and recent jobs."""
    try:
        window_hours = max(1, min(int(window_hours or 24), 168))
    except (TypeError, ValueError):
        window_hours = 24
    return external_overview(db, current_user.tenant_id, window_hours)


@router.get("/external/farms", response_model=list[FarmSummary])
def list_external_farms(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """This tenant's registered external orchestrator farms (never a secret)."""
    return list_farms(db, current_user.tenant_id)


@router.post("/external/farms", response_model=FarmSummary, status_code=201)
def create_external_farm(
    body: FarmCreateRequest = Body(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Register one farm for this tenant; the credential goes to Vault only."""
    _require_tenant_admin(current_user)

    kind = (body.kind or "").strip()
    if kind not in FARM_KINDS:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_farm_kind",
                    "message": "Farm kind must be kubernetes, qube or deadline."})

    name = (body.name or "").strip()
    if not name or len(name) > FARM_NAME_MAX:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_farm_name",
                    "message": "Farm name must be 1-120 characters."})

    endpoint = _validate_farm_endpoint((body.endpoint or "").strip())

    credential = None
    if body.credential is not None:
        secret = body.credential.get_secret_value()
        if not secret or len(secret) > FARM_CREDENTIAL_MAX:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_farm_credential",
                        "message": "Farm credential must be 1-4096 characters."})
        credential = secret

    try:
        row = create_farm(db, current_user.tenant_id, kind, name, endpoint,
                          credential)
    except ExternalFarmVaultError:
        # The service already rolled the row back (fail closed) and its message
        # names no path/token; we do not chain it, so nothing can leak either.
        raise HTTPException(
            status_code=503,
            detail={"code": "vault_unavailable",
                    "message": "Farm credential could not be stored. "
                               "Try again later."}) from None
    except DuplicateFarmNameError:
        raise HTTPException(
            status_code=409,
            detail={"code": "duplicate_farm_name",
                    "message": "A farm with this name is already registered "
                               "for this tenant."}) from None
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_farm",
                    "message": "Farm kind, name and endpoint are required."}) from None

    _safe_audit(db, current_user, models.AuditAction.EXTERNAL_FARM_CREATED,
                row.id, {"kind": kind, "name": name})

    return FarmSummary(id=row.id, kind=row.kind, name=row.name,
                       endpoint=row.endpoint, is_active=bool(row.is_active),
                       has_credential=row.vault_path is not None,
                       created_at=row.created_at.isoformat()
                       if row.created_at is not None else None)


@router.delete("/external/farms/{farm_id}", response_model=FarmDeleteResponse)
def delete_external_farm(
    farm_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Drop one farm and its Vault secret. A foreign tenant's id is not-found."""
    _require_tenant_admin(current_user)
    try:
        found = delete_farm(db, current_user.tenant_id, farm_id)
    except ExternalFarmVaultError:
        raise HTTPException(
            status_code=503,
            detail={"code": "vault_unavailable",
                    "message": "Farm credential could not be removed. "
                               "Try again later."}) from None
    if not found:
        # Identical body for a missing id and another tenant's id: no oracle
        # about which ids exist elsewhere.
        raise HTTPException(
            status_code=404,
            detail={"code": "farm_not_found",
                    "message": "No such farm is registered for this tenant."}) from None

    _safe_audit(db, current_user, models.AuditAction.EXTERNAL_FARM_DELETED,
                farm_id, {"farm_id": farm_id})
    return FarmDeleteResponse(id=farm_id, deleted=True)
