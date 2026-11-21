"""GY.C4.MFA — the current user's 2FA status.

GET /v1/me/mfa — mounted under /v1/me by routers/me/__init__.py.

Returns the enrollment state read from the Keycloak admin API (credential
type / created date / device label only — no secret is ever returned) plus
the MOD-branded enrollment URL. The Keycloak-branded account console is never
linked from MOD UI: `setup_url` is the Dashboard-client authorization URL with
kc_action=CONFIGURE_TOTP, rendered by the modtech login theme
(login-config-totp.ftl), and the Dashboard's existing kc_action_status return
handling shows the success toast.

All work lives in services/mfa_service.py — this router only resolves the
caller and calls the service (lint R5).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from keycloak_auth import keycloak_admin_client
try:
    from services import mfa_enforcement
    from services import mfa_service
except ImportError:
    mfa_enforcement = None
    mfa_service = None

logger = logging.getLogger(__name__)

router = APIRouter()


class MfaCredential(BaseModel):
    id: Optional[str] = None
    type: Optional[str] = None
    device_label: Optional[str] = None
    created_at: Optional[str] = None


class MfaStatusResponse(BaseModel):
    enrolled: bool
    credential_created_at: Optional[str] = None
    device_label: Optional[str] = None
    required: bool
    exempt: bool
    setup_url: str
    change_password_url: Optional[str] = None
    keycloak_user_id: Optional[str] = None
    credentials: List[MfaCredential] = []


@router.get("/mfa", response_model=MfaStatusResponse)
def get_my_mfa_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> MfaStatusResponse:
    """2FA status for the signed-in user. No secret is ever returned."""
    if mfa_service is None:
        payload = {
            "enrolled": False,
            "credential_created_at": None,
            "device_label": None,
            "required": False,
            "exempt": True,
            "setup_url": "",
            "change_password_url": None,
            "keycloak_user_id": None,
            "credentials": [],
        }
    else:
        try:
            payload = mfa_service.mfa_status(db, current_user, keycloak_admin_client)
        except Exception:  # noqa: BLE001 — never leak raw provider errors
            logger.warning("mfa status read failed for user %s", current_user.id)
            raise HTTPException(
                status_code=502,
                detail="could not read 2FA status from the identity provider")
    return MfaStatusResponse(**payload)


class MfaEnforcementStatusResponse(BaseModel):
    """GSEC FX.53 (Q36=A) — the enforcement state behind the Dashboard
    banner. ``required`` says the install demands a 2FA factor from
    this user; ``grace_days_left`` is the countdown (null = no grace
    clock yet or not required). The banner shows while required and not
    enrolled and grace_days_left > 0."""
    required: bool
    enrolled: bool
    grace_days_left: Optional[int] = None
    exempt: bool
    factor: Optional[str] = None


@router.get("/mfa/enforcement",
            response_model=MfaEnforcementStatusResponse)
def get_my_mfa_enforcement(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> MfaEnforcementStatusResponse:
    """FX.53 — current user's MFA enforcement state (banner endpoint)."""
    if mfa_enforcement is None:
        payload = {
            "required": False,
            "enrolled": False,
            "grace_days_left": None,
            "exempt": True,
            "factor": None,
        }
    else:
        try:
            payload = mfa_enforcement.enforcement_status(
                db, current_user, keycloak_admin_client)
        except Exception:  # noqa: BLE001 — never leak raw provider errors
            logger.warning("mfa enforcement status read failed for user %s",
                           current_user.id)
            raise HTTPException(
                status_code=502,
                detail="could not read MFA enforcement state from the "
                       "identity provider")
    return MfaEnforcementStatusResponse(**payload)
