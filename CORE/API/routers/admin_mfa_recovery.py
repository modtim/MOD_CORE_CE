"""GSEC FX.88 p3 — platform-admin review of last-admin MFA recovery requests.

Platform admin only. Two-person rule: the platform admin who records the
callback (``POST .../{id}/callback-verified``) and the platform admin who
approves (``POST .../{id}/approve``) MUST be different people — 409
otherwise (services/mfa_recovery.py:approve_request). Approval performs the
actual Keycloak reset (same helper routers/tenant/mfa_reset.py p2a uses) and
writes one AUTH_MFA_RESET audit row. ``POST .../{id}/reject`` is the
terminal dead-end either staff step may use.

Business logic lives in services/mfa_recovery.py; this router is HTTP-only
(parse/validate input, enforce the platform-admin gate, map ServiceError ->
HTTPException) per the services/_base.py layering convention.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from keycloak_auth import keycloak_admin_client
from services import mfa_recovery
from services._base import ServiceError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/mfa-recovery-requests",
                    tags=["Admin — MFA Recovery"])


def require_platform_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Platform admin only")
    return current_user


def _raise_http(exc: ServiceError) -> None:
    status_code = {"not_found": 404, "conflict": 409,
                   "bad_request": 400}.get(exc.code, 500)
    raise HTTPException(status_code=status_code, detail=exc.message)


class CallbackVerifiedBody(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class ApproveBody(BaseModel):
    pass


class RejectBody(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


@router.get("", response_model=list)
def list_mfa_recovery_requests(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_platform_admin),
):
    return mfa_recovery.list_requests(db, status=status)


@router.post("/{request_id}/callback-verified", response_model=dict)
def mark_callback_verified(
    request_id: int,
    body: CallbackVerifiedBody,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_platform_admin),
):
    """Staff records they phoned the security-contact number on file."""
    try:
        return mfa_recovery.mark_callback_verified(
            db, request_id=request_id, actor=current_user, note=body.note)
    except ServiceError as exc:
        _raise_http(exc)


@router.post("/{request_id}/approve", response_model=dict)
def approve_mfa_recovery_request(
    request_id: int,
    body: ApproveBody,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_platform_admin),
):
    """Second platform-admin approval — performs the Keycloak reset.

    A KC failure (``kc_mfa.KcError``) happens BEFORE the request row or any
    audit row is updated (services/mfa_recovery.py:approve_request commits
    only after the KC call succeeds) and maps to 502 here — same
    "no false success" rule as routers/tenant/mfa_reset.py p2a."""
    from services import kc_mfa
    try:
        return mfa_recovery.approve_request(
            db, request_id=request_id, actor=current_user,
            kc_client=keycloak_admin_client)
    except ServiceError as exc:
        _raise_http(exc)
    except kc_mfa.KcError as exc:
        logger.warning("FX.88 p3 MFA recovery approve KC failure: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Keycloak is unavailable — the MFA reset was not applied")


@router.post("/{request_id}/reject", response_model=dict)
def reject_mfa_recovery_request(
    request_id: int,
    body: RejectBody,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_platform_admin),
):
    try:
        return mfa_recovery.reject_request(
            db, request_id=request_id, actor=current_user,
            reason=body.reason)
    except ServiceError as exc:
        _raise_http(exc)
