"""GH.2 — tenant-admin surface for burst cloud accounts (group H).

Endpoints (registered under /burst in main.py → /burst/accounts/*):

  GET    /burst/providers            the provider credential catalog
  GET    /burst/accounts             list the tenant's live accounts
  POST   /burst/accounts             create an account (secret by NAME)
  PATCH  /burst/accounts/{id}        update mutable fields
  DELETE /burst/accounts/{id}        soft-delete
  POST   /burst/accounts/{id}/probe  dry-run permission probe

  Read-only cloud-burst dashboard surface (GH.8a):
  GET    /burst/sessions             the tenant's burst sessions (+ fees)
  GET    /burst/ledger               the tenant's burst fee ledger
  GET    /burst/summary              accounts / active sessions / monthly fees

Auth: active + onboarded user, tenant-admin only (mirrors the SCIM/SSO
surface). Every read/write is tenant-scoped on ``current_user.tenant_id``
— a foreign tenant's account reads as 404.

Secrets: `secret_ref` is the NAME of a tenant secret. Responses carry
only that name and probe metadata — never the credential value.
"""
from __future__ import annotations

import logging
import os
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services import burst_accounts
from services import burst_provider_catalog

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Burst Accounts"],
    dependencies=[Depends(require_active_and_onboarded_user)],
)


def _require_tenant_admin(user: models.User) -> None:
    """Burst account config is tenant-admin only — mirrors the SCIM/SSO
    surface (routers/admin_scim_tokens._require_tenant_admin)."""
    if getattr(user, "is_platform_admin", False):
        return
    if user.parent_user_id is None:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "tenant_admin_required",
                "message": "Only tenant admins can manage burst accounts."},
    )


# --- schemas -----------------------------------------------------------------

def _is_production_mode() -> bool:
    """SaaS/production mode = DEPLOYMENT_MODE=saas (the codebase's existing
    decision; see main.py / utils/entitlements.is_saas). The dev-only
    'fake' provider is hidden from the /providers catalog in this mode."""
    return os.getenv("DEPLOYMENT_MODE", "onprem").lower() == "saas"


def _PROVIDERS() -> dict:
    """The burst provider registry (name -> factory), read live so a
    provider registered after this module imported still counts as
    known (dev/tests register 'fake' at import time of the adapters)."""
    import services.burst_provider as bp
    return bp._PROVIDERS


def _validate_provider(name: str) -> None:
    """422 with the known-name list when `name` is not a registered
    burst provider — a typo fails here, not at the next probe."""
    if name not in _PROVIDERS():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "unknown_burst_provider",
                    "message": f"unknown burst provider {name!r}; "
                               f"known providers: {_PROVIDERS()}"},
        )


class BurstAccountCreate(BaseModel):
    provider: str = Field(
        ..., min_length=1, max_length=50,
        description="Registered burst-provider name — the real clouds are "
                    "ec2, azure, gcp, lambda, oci ('libcloud' for the "
                    "generic driver, 'fake' in dev/tests). See "
                    "GET /burst/providers for each provider's credential "
                    "fields; the provider is validated against the "
                    "registry and unknown names are rejected with 422.",
    )
    secret_ref: str = Field(
        ..., min_length=1, max_length=255,
        description="NAME of the tenant secret holding the cloud "
                    "credential — never the credential itself.",
    )
    region: Optional[str] = Field(None, max_length=100)
    limits: Optional[dict] = Field(
        None,
        description="Policy block: {max_instances, max_gpu_hours_month, "
                    "allowed_instance_types}.",
    )


class BurstAccountUpdate(BaseModel):
    provider: Optional[str] = Field(None, min_length=1, max_length=50)
    secret_ref: Optional[str] = Field(None, min_length=1, max_length=255)
    region: Optional[str] = Field(None, max_length=100)
    limits: Optional[dict] = None


class BurstAccountOut(BaseModel):
    """Serialization surface — deliberately has NO secret material
    (`secret_ref` is the secret's NAME; the value never leaves the box)."""
    id: int
    tenant_id: int
    provider: str
    secret_ref: str
    region: Optional[str] = None
    limits: dict = Field(default_factory=dict)
    status: str
    last_probe_at: Optional[str] = None
    last_probe_error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _out(account: models.TenantBurstAccount) -> BurstAccountOut:
    return BurstAccountOut(**burst_accounts.account_dict(account))


class BurstSessionOut(BaseModel):
    """One burst worker's meter row for the dashboard. `provider` is joined
    from the account; `fees_charged_credits` is the positive sum of the
    session's management-fee debits (0 if the meter never fired). No
    credential material — only the session's identity + meter state."""
    id: int
    account_id: Optional[int] = None
    provider: Optional[str] = None
    instance_id: Optional[str] = None
    worker_name: Optional[str] = None
    gpu_class: str
    status: str
    joined_at: Optional[str] = None
    left_at: Optional[str] = None
    fees_charged_credits: int = 0


class BurstFeeLedgerEntry(BaseModel):
    """One burst management-fee debit (a positive `amount_credits`; the
    ledger stores it negative). `session_id` is recovered from the meter's
    ledger note; `description` is that note verbatim (no secret material)."""
    id: int
    created_at: Optional[str] = None
    session_id: Optional[int] = None
    amount_credits: int
    description: Optional[str] = None


class BurstSummaryOut(BaseModel):
    accounts: int
    active_sessions: int
    fees_this_month_credits: int


# --- provider catalog --------------------------------------------------------

@router.get("/providers", response_model=list[dict])
def list_providers(
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """The credential catalog for the Connect form: for each registered
    burst provider, its label, the credential fields its factory accepts
    (with secret/required/multiline flags), and a region hint. Metadata
    only — never returns any secret value. The dev-only `fake` provider
    is hidden in production (SaaS) mode."""
    _require_tenant_admin(current_user)
    return burst_provider_catalog.list_providers(
        include_dev=not _is_production_mode()
    )


# --- endpoints ---------------------------------------------------------------

@router.get("/accounts", response_model=list[BurstAccountOut])
def list_accounts(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    _require_tenant_admin(current_user)
    return [
        _out(a)
        for a in burst_accounts.list_accounts(db, tenant_id=current_user.tenant_id)
    ]


@router.post("/accounts", response_model=BurstAccountOut,
             status_code=status.HTTP_201_CREATED)
def create_account(
    body: BurstAccountCreate,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    _require_tenant_admin(current_user)
    _validate_provider(body.provider)
    try:
        acct = burst_accounts.create_account(
            db, tenant_id=current_user.tenant_id, user=current_user,
            provider=body.provider, secret_ref=body.secret_ref,
            region=body.region, limits=body.limits,
        )
    except ValueError as e:
        # Controlled validation/conflict message from burst_accounts.create_account
        # (not a raw internal exception) — safe to surface via the "code" contract.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"code": "burst_account_conflict",
                                    "message": e.args[0] if e.args else "Cloud account conflict"})
    return _out(acct)


@router.patch("/accounts/{account_id}", response_model=BurstAccountOut)
def update_account(
    account_id: int,
    body: BurstAccountUpdate,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    _require_tenant_admin(current_user)
    if body.provider is not None:
        _validate_provider(body.provider)
    try:
        acct = burst_accounts.update_account(
            db, account_id=account_id, tenant_id=current_user.tenant_id,
            user=current_user,
            provider=body.provider, secret_ref=body.secret_ref,
            region=body.region, limits=body.limits,
        )
    except ValueError as e:
        # Controlled validation message from burst_accounts.update_account
        # (not a raw internal exception) — same 409 shape as POST /accounts.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"code": "burst_account_conflict",
                                    "message": e.args[0] if e.args else "Cloud account conflict"})
    if acct is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "burst_account_not_found"})
    return _out(acct)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: int,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    _require_tenant_admin(current_user)
    acct = burst_accounts.soft_delete_account(
        db, account_id=account_id, tenant_id=current_user.tenant_id,
        user=current_user,
    )
    if acct is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "burst_account_not_found"})
    return None


@router.post("/accounts/{account_id}/probe", response_model=BurstAccountOut)
def probe_account(
    account_id: int,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    _require_tenant_admin(current_user)
    acct = burst_accounts.get_account(
        db, account_id=account_id, tenant_id=current_user.tenant_id,
    )
    if acct is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "burst_account_not_found"})
    probed = burst_accounts.probe_account(db, acct, user=current_user)
    return _out(probed)


# --- read-only cloud-burst dashboard surface (GH.8a) -------------------------

@router.get("/sessions", response_model=list[BurstSessionOut])
def list_sessions(
    status_filter: Literal["active", "all"] = Query(
        "active", alias="status",
        description="'active' (live meters) or 'all' (any status)."),
    limit: int = Query(50, ge=1, le=1000),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """The tenant's burst sessions, newest first, with each session's
    management-fee total. `provider` is joined from the account; a foreign
    tenant's session is invisible (tenant-scoped in the service)."""
    _require_tenant_admin(current_user)
    return burst_accounts.list_sessions(
        db, tenant_id=current_user.tenant_id, status=status_filter,
        limit=limit,
    )


@router.get("/ledger", response_model=list[BurstFeeLedgerEntry])
def list_fee_ledger(
    limit: int = Query(100, ge=1, le=1000),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """The tenant's burst management-fee ledger entries, newest first
    (`amount_credits` is the positive charge; the ledger stores debits
    negative). `session_id` is recovered from the meter's ledger note."""
    _require_tenant_admin(current_user)
    return burst_accounts.list_fee_ledger(
        db, tenant_id=current_user.tenant_id, limit=limit,
    )


@router.get("/summary", response_model=BurstSummaryOut)
def burst_summary(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """Cloud-burst Dashboard rollup for the tenant: live accounts, active
    sessions, and management fees charged in the current UTC month."""
    _require_tenant_admin(current_user)
    return burst_accounts.summary(db, tenant_id=current_user.tenant_id)
