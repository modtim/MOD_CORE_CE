"""GSEC FX.88 p3 — last-admin MFA recovery workflow.

When a tenant's ONLY active admin loses MFA, nobody IN THE TENANT can run
the FX.88 p2a tenant-admin reset (routers/tenant/mfa_reset.py) — every path
there needs an authenticated caller who is either that same locked-out
admin (self-reset is refused, 409) or another tenant admin (there isn't
one). This module is the out-of-band path for exactly that gap:

  1. Public, unauthenticated intake — `create_recovery_request` — called
     from routers/public_mfa_recovery.py. Always returns the same shape
     (the router always answers 202) regardless of whether a row was
     written, so the endpoint never reveals which tenants/emails exist. A
     row is written ONLY when `requester_email` resolves to an ACTIVE admin
     (``parent_user_id IS NULL``, ``status == ACTIVE``) of the target tenant
     AND that tenant has no OTHER active admin — the "last admin" gate.
     On write, the tenant's registered security contact
     (services/tenant_security_contact_service.py) is emailed — never the
     requester — telling them a recovery was requested and MOD staff will
     call back on the number on file.

  2. Platform-admin review — `list_requests`, `mark_callback_verified`,
     `approve_request`, `reject_request` — called from
     routers/admin_mfa_recovery.py. Two-person rule: the platform admin who
     records the callback and the platform admin who approves MUST be
     different people (409 otherwise) — mirrors the break-glass review
     shape (routers/admin_break_glass.py) but inverted (verify-then-approve
     instead of act-then-review) because here the Keycloak write itself
     must wait for the second approval, not just its post-hoc audit.
     Approval performs the SAME Keycloak reset the p2a route uses
     (services.kc_mfa.reset_mfa_credentials + revoke_user_sessions) and
     writes one AUTH_MFA_RESET audit row (details carry
     {target_user_id, request_id, by, second_approver}).

Expiry (``MFA_RECOVERY_REQUEST_TTL_HOURS`` = 72h from creation) is checked
LAZILY on read/approve — no background sweep — via `_effective_status`;
once expired, a request can only be read, never approved/rejected/
callback-verified (409).

Every row is append-only (SoftDeleteMixin `deleted_at`, ISO 27001 — no hard
deletes anywhere in this module).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models
from services._base import ConflictError, NotFoundError, ServiceError

logger = logging.getLogger(__name__)

TTL_HOURS = models.MFA_RECOVERY_REQUEST_TTL_HOURS

STATUS_REQUESTED = "requested"
STATUS_CALLBACK_VERIFIED = "callback_verified"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_COMPLETED = "completed"
STATUS_EXPIRED = "expired"

# The same uniform response for every outcome of the public endpoint — no
# tenant/user enumeration, ever.
PUBLIC_RESPONSE = {"status": "received"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_active_admin(u: models.User) -> bool:
    return bool(u) and u.parent_user_id is None and \
        u.status == models.UserStatus.ACTIVE


def _active_admins(db: Session, tenant_id: int) -> List[models.User]:
    return (
        db.query(models.User)
        .filter(
            models.User.tenant_id == tenant_id,
            models.User.parent_user_id.is_(None),
            models.User.status == models.UserStatus.ACTIVE,
        )
        .all()
    )


def _resolve_tenant(db: Session, tenant_slug: Optional[str],
                    email: Optional[str]) -> Optional[models.Tenant]:
    """Resolve the target tenant. ``tenant_slug`` matches ``Tenant.name``
    (this codebase has no dedicated slug column); when absent, ``email`` —
    which doubles as ``requester_email`` on the request body — resolves the
    tenant via whichever user row owns that email. Returns None (never
    raises) on any miss so the caller can fold every "not found" case into
    the same uniform public response."""
    if tenant_slug:
        return db.query(models.Tenant).filter(
            models.Tenant.name == tenant_slug).first()
    if email:
        u = db.query(models.User).filter(
            models.User.email == email).first()
        if u:
            return db.query(models.Tenant).filter(
                models.Tenant.id == u.tenant_id).first()
    return None


def _security_contact_snapshot(db: Session, tenant: models.Tenant) -> Optional[str]:
    """CE: services.tenant_security_contact_service is Paid (open-core/manifest.yaml
    deny), so the bare import used to raise ModuleNotFoundError on the CE request
    path -> a 500 and a row written with no contact. Degrade to "no contact
    configured" — the exact shape a tenant with no security contact already
    produces (security_contact_email stays NULL, _notify_security_contact returns
    early). Paid behaviour is unchanged."""
    from utils.paid_import import optional_paid
    contact_service = optional_paid("services.tenant_security_contact_service")
    if contact_service is None:
        logger.info(
            "FX.88: security-contact snapshot skipped (not in this edition): "
            "services.tenant_security_contact_service")
        return None
    recipients = contact_service.resolve_security_recipients(db, tenant)
    return recipients[0]["email"] if recipients else None


def _notify_security_contact(db: Session, request_row: "models.MfaRecoveryRequest",
                             tenant: models.Tenant) -> None:
    """Best-effort email to the security contact (never the requester) —
    a mail outage must never fail or roll back the request write."""
    contact_email = request_row.security_contact_email
    if not contact_email:
        return
    try:
        from services import email_send_api
        email_send_api.prepare_send(
            db,
            tenant_id=tenant.id,
            user_id=request_row.target_user_id,
            app_id=None,
            to=[contact_email],
            cc=[],
            bcc=[],
            subject="MOD: MFA recovery requested for your tenant's admin",
            text=(
                "A last-admin MFA recovery was requested for your tenant. "
                "MOD staff will call back on the number on file to verify "
                "the request before any reset is approved. If you did not "
                "expect this, contact MOD support immediately."
            ),
            html=None,
            attachments=None,
        )
    except Exception as exc:  # noqa: BLE001 — never block request creation
        logger.warning(
            "FX.88 p3: security-contact notification failed (tenant %s): %s",
            tenant.id, exc)


def create_recovery_request(
    db: Session, *, tenant_slug: Optional[str], requester_email: str,
    reason: str,
) -> Dict[str, Any]:
    """Create the row (or not) and ALWAYS return PUBLIC_RESPONSE — the
    router must return this verbatim regardless of which branch ran, so
    the endpoint never leaks tenant/user existence."""
    tenant = _resolve_tenant(db, tenant_slug, requester_email)
    if tenant is None:
        return dict(PUBLIC_RESPONSE)

    requester = db.query(models.User).filter(
        models.User.tenant_id == tenant.id,
        models.User.email == requester_email,
    ).first()
    if requester is None or not _is_active_admin(requester):
        return dict(PUBLIC_RESPONSE)

    admins = _active_admins(db, tenant.id)
    if len(admins) != 1 or admins[0].id != requester.id:
        # Either another active admin exists (they can self-serve the
        # p2a reset) or something is inconsistent — never write a row.
        return dict(PUBLIC_RESPONSE)

    contact_email = _security_contact_snapshot(db, tenant)
    row = models.MfaRecoveryRequest(
        tenant_id=tenant.id,
        target_user_id=requester.id,
        requester_email=requester_email,
        security_contact_email=contact_email,
        reason=(reason or "")[:2000],
        status=STATUS_REQUESTED,
        expires_at=_now() + timedelta(hours=TTL_HOURS),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    _notify_security_contact(db, row, tenant)

    return dict(PUBLIC_RESPONSE)


def _effective_status(row: "models.MfaRecoveryRequest") -> str:
    """Lazy expiry: a non-terminal row past its expires_at reads as
    'expired' without a background job. Terminal states never flip."""
    if row.status in (STATUS_APPROVED, STATUS_COMPLETED, STATUS_REJECTED,
                      STATUS_EXPIRED):
        return row.status
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and _now() > expires_at:
        return STATUS_EXPIRED
    return row.status


def _get_request(db: Session, request_id: int) -> "models.MfaRecoveryRequest":
    row = db.query(models.MfaRecoveryRequest).filter(
        models.MfaRecoveryRequest.id == request_id,
        models.MfaRecoveryRequest.deleted_at.is_(None),
    ).first()
    if row is None:
        raise NotFoundError("MFA recovery request not found")
    return row


def _sync_expiry(db: Session, row: "models.MfaRecoveryRequest") -> str:
    """Persist the lazily-computed expired status (so list views are
    consistent) and return it."""
    eff = _effective_status(row)
    if eff == STATUS_EXPIRED and row.status != STATUS_EXPIRED:
        row.status = STATUS_EXPIRED
        db.commit()
        db.refresh(row)
    return eff


def _serialize(row: "models.MfaRecoveryRequest", status: str) -> Dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "target_user_id": row.target_user_id,
        "requester_email": row.requester_email,
        "security_contact_email": row.security_contact_email,
        "reason": row.reason,
        "status": status,
        "callback_verified_by_user_id": row.callback_verified_by_user_id,
        "callback_verified_at":
            row.callback_verified_at.isoformat() if row.callback_verified_at else None,
        "callback_note": row.callback_note,
        "approved_by_user_id": row.approved_by_user_id,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "rejected_by_user_id": row.rejected_by_user_id,
        "rejected_at": row.rejected_at.isoformat() if row.rejected_at else None,
        "rejection_reason": row.rejection_reason,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_requests(db: Session, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
    q = db.query(models.MfaRecoveryRequest).filter(
        models.MfaRecoveryRequest.deleted_at.is_(None))
    rows = q.order_by(models.MfaRecoveryRequest.created_at.desc()).all()
    out = []
    for row in rows:
        eff = _sync_expiry(db, row)
        if status and eff != status:
            continue
        out.append(_serialize(row, eff))
    return out


def mark_callback_verified(
    db: Session, *, request_id: int, actor: models.User, note: str,
) -> Dict[str, Any]:
    row = _get_request(db, request_id)
    eff = _sync_expiry(db, row)
    if eff == STATUS_EXPIRED:
        raise ConflictError("This recovery request has expired")
    if eff != STATUS_REQUESTED:
        raise ConflictError(
            f"Request is in status '{eff}', expected 'requested'")
    row.status = STATUS_CALLBACK_VERIFIED
    row.callback_verified_by_user_id = actor.id
    row.callback_verified_at = _now()
    row.callback_note = (note or "")[:2000]
    db.commit()
    db.refresh(row)
    return _serialize(row, row.status)


def approve_request(
    db: Session, *, request_id: int, actor: models.User, kc_client: Any,
) -> Dict[str, Any]:
    """Second-approver step: performs the actual Keycloak MFA reset and
    marks the request completed. Raises ConflictError (-> 409) if the
    request hasn't been callback-verified, has expired, or the approver is
    the SAME platform admin who did the callback verification."""
    row = _get_request(db, request_id)
    eff = _sync_expiry(db, row)
    if eff == STATUS_EXPIRED:
        raise ConflictError("This recovery request has expired")
    if eff != STATUS_CALLBACK_VERIFIED:
        raise ConflictError(
            f"Request is in status '{eff}', expected 'callback_verified'")
    if row.callback_verified_by_user_id == actor.id:
        raise ConflictError(
            "The second approval must be a different platform admin from "
            "the one who verified the callback")

    target = db.query(models.User).filter(
        models.User.id == row.target_user_id).first()
    if target is None:
        raise NotFoundError("Target user not found")

    from services import kc_mfa, mfa_service
    import requests as _requests
    token = kc_client._get_token()
    base = mfa_service._base_from_admin_url(kc_client.admin_api_url)
    realm = mfa_service._realm_from_admin_url(kc_client.admin_api_url)
    session = _requests.Session()
    kc_uid = target.keycloak_user_id or kc_mfa.find_user_id_by_email(
        session, base, realm, target.email, token)
    if not kc_uid:
        raise NotFoundError("Target user not found in Keycloak")

    result = kc_mfa.reset_mfa_credentials(
        session, base, realm, kc_uid, token, queue_reenroll=True)
    sessions_revoked = kc_client.revoke_user_sessions(kc_uid)

    row.approved_by_user_id = actor.id
    row.approved_at = _now()
    row.status = STATUS_COMPLETED
    row.completed_at = _now()

    from audit import log_permission_audit
    log_permission_audit(
        db=db,
        action=models.AuditAction.AUTH_MFA_RESET,
        actor_user_id=actor.id,
        tenant_id=row.tenant_id,
        resource_type="user",
        resource_id=target.id,
        target_user_id=target.id,
        metadata={
            "target_user_id": target.id,
            "request_id": row.id,
            "by": actor.email,
            "second_approver": actor.email,
            "initiated_by": "last_admin_recovery",
        },
        request=None,
    )
    db.commit()
    db.refresh(row)

    return {
        **_serialize(row, row.status),
        "otp_credentials_removed": result.get("otp_credentials_removed", []),
        "webauthn_credentials_removed":
            result.get("webauthn_credentials_removed", []),
        "reenroll_queued": bool(result.get("reenroll_queued")),
        "sessions_revoked": bool(sessions_revoked),
    }


def reject_request(
    db: Session, *, request_id: int, actor: models.User, reason: str,
) -> Dict[str, Any]:
    row = _get_request(db, request_id)
    eff = _sync_expiry(db, row)
    if eff == STATUS_EXPIRED:
        raise ConflictError("This recovery request has expired")
    if eff in (STATUS_APPROVED, STATUS_COMPLETED, STATUS_REJECTED):
        raise ConflictError(f"Request is already in status '{eff}'")
    row.status = STATUS_REJECTED
    row.rejected_by_user_id = actor.id
    row.rejected_at = _now()
    row.rejection_reason = (reason or "")[:2000]
    db.commit()
    db.refresh(row)
    return _serialize(row, row.status)
