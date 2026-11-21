"""GY.C4.MFA — admin 2FA reset (replaces the Keycloak admin console).

One route (mounted under /v1 by main.py — no /v1 here):

    POST /v1/admin/users/{user_id}/mfa/reset — platform admin (any tenant) or
    tenant admin (own tenant), same authz shape as the existing admin user
    actions (deactivate / reactivate in routers/users.py): the route resolves
    the caller and the service resolves the target + tenant scope.

The service removes the target's OTP credential(s) in Keycloak, queues
CONFIGURE_TOTP so the user re-enrolls on the MOD-branded login page, writes
the AUTH_MFA_RESET audit row (append-only — no DB row is hard-deleted) and
notifies the active platform admins; that audit row is what the SIEM export
(services/siem_export.py) ships.

Exempt accounts (e2e_service_principals members, service-account-*,
*@service.local, api_service, platform-service) are refused with 409 — the
10-0x "2FA killed everything" cause: those principals authenticate by
client-credentials / direct grant and can never complete an enrollment.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from keycloak_auth import keycloak_admin_client
from services import kc_mfa
from services import mfa_service
from services._base import ServiceError

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Admin - MFA"],
    prefix="/admin/users",
)


class MfaResetBody(BaseModel):
    reason: str = Field(default="", max_length=500)


class MfaResetResponse(BaseModel):
    user_id: int
    keycloak_user_id: Optional[str] = None
    otp_credentials_removed: List[str] = []
    webauthn_credentials_removed: List[str] = []
    reenroll_queued: bool
    audit_id: Optional[int] = None
    notified_admins: int = 0


def _raise_http(exc: ServiceError) -> None:
    status_code = {
        "not_found": 404,
        "forbidden": 403,
        "conflict": 409,
        "bad_request": 400,
        "unprocessable": 422,
    }.get(exc.code, 500)
    raise HTTPException(status_code=status_code, detail=exc.message)


@router.post("/{user_id}/mfa/reset", response_model=MfaResetResponse)
def reset_user_mfa_endpoint(
    user_id: int,
    body: MfaResetBody,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> Dict[str, Any]:
    """Remove a user's 2FA authenticator and queue re-enrollment on the
    MOD-branded login page. Audited (auth.mfa_reset) + admin notification."""
    try:
        result = mfa_service.reset_user_mfa(
            db, current_user, user_id, body.reason,
            keycloak_admin_client, request=None)
    except ServiceError as exc:
        _raise_http(exc)
    except kc_mfa.KcUserMissing as exc:
        # BUG.B5 — the DB row's keycloak_user_id is stale (the realm has no
        # such user: 37/100 rows are like this on 10-08). That is a data
        # problem, not a provider outage: say so, with the id, instead of
        # the blind 502 the admin could not act on.
        logger.warning(
            "mfa reset: user %s is not in the Keycloak realm (kc id %s) — "
            "stale keycloak_user_id; reset by %s",
            user_id, exc.user_id, current_user.id)
        raise HTTPException(
            status_code=404,
            detail="user is not bound to the Keycloak realm — their "
                   f"keycloak_user_id ({exc.user_id}) has no realm user; "
                   "re-provision the account before resetting 2FA") from exc
    except kc_mfa.KcError as exc:
        # BUG.B5: internal_http_error logs the real KC method/path/status
        # (KcError carries only those — never a token, header or credential
        # value) plus an error_id, so the next 502 is diagnosable.
        from utils import http_errors
        raise http_errors.internal_http_error(
            502, "could not reset 2FA in the identity provider", exc,
            log=logger) from exc
    except Exception as exc:  # noqa: BLE001 — never leak raw provider errors
        logger.exception(
            "mfa reset failed for user %s by %s: %s", user_id,
            current_user.id, exc)
        raise HTTPException(
            status_code=502,
            detail="could not reset 2FA in the identity provider") from exc
    return MfaResetResponse(**result)


# --------------------------------------------------------------------------- #
# BUG.B5b — admin "Re-link account" (identity rebinding for stale KC ids)
# --------------------------------------------------------------------------- #

class IdentityRelinkBody(BaseModel):
    reason: str = Field(min_length=5, max_length=500)
    confirm: bool


class IdentityRelinkResponse(BaseModel):
    action: str
    user_id: int
    old_keycloak_user_id: Optional[str] = None
    new_keycloak_user_id: Optional[str] = None
    matched_by: Optional[str] = None
    reason_for_reprovision: Optional[str] = None


class IdentityStatusResponse(BaseModel):
    linked: bool
    keycloak_user_id: Optional[str] = None
    candidate: Dict[str, Any]


@router.get("/{user_id}/identity/status", response_model=IdentityStatusResponse)
def get_identity_status_endpoint(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> Dict[str, Any]:
    """Is the target's keycloak_user_id actually in the realm, and is there
    exactly one verified exact-email candidate? The UI shows the Re-link
    button only when linked is false."""
    try:
        return mfa_service.identity_status(db, current_user, user_id,
                                           keycloak_admin_client)
    except ServiceError as exc:
        _raise_http(exc)
    except kc_mfa.KcError as exc:
        from utils import http_errors
        raise http_errors.internal_http_error(
            502, "could not read the identity provider", exc,
            log=logger) from exc


@router.post("/{user_id}/identity/relink", response_model=IdentityRelinkResponse)
def relink_identity_endpoint(
    user_id: int,
    body: IdentityRelinkBody,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> Dict[str, Any]:
    """Rebind a stale keycloak_user_id: exact+verified email match when safe,
    otherwise re-provision a fresh realm account. Audited on every outcome
    (ACCOUNT_RELINKED / ACCOUNT_REPROVISIONED / ACCOUNT_RELINK_REFUSED)."""
    try:
        result = mfa_service.relink_identity(
            db, current_user, user_id, body.reason, body.confirm,
            keycloak_admin_client)
    except ServiceError as exc:
        _raise_http(exc)
    except kc_mfa.KcUserMissing as exc:
        raise HTTPException(
            status_code=404,
            detail=f"keycloak_user_id ({exc.user_id}) has no realm user") from exc
    except kc_mfa.KcError as exc:
        from utils import http_errors
        raise http_errors.internal_http_error(
            502, "could not reach the identity provider", exc,
            log=logger) from exc
    return IdentityRelinkResponse(**result)
