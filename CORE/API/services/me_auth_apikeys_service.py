# API/services/me_auth_apikeys_service.py
"""Business logic for the `/me` auth/session domain (routers/me/auth_apikeys.py).

Companion to `crud/me_auth_apikeys.py` (the ONLY place this domain's
`db.query(models.UserSession)` runs). The router
(`routers/me/auth_apikeys.py`) stays HTTP-only: parse input, call the
function here, return the result. FastAPI's `Depends()` auth guard
(`require_active_and_onboarded_user`) stays on the router -- it resolves
before this function is ever called.

FX.112 PART 1: sign-outs are audited (SESSION_SIGN_OUT_*, audit-only)
and propagated to Keycloak so the identity provider forgets the
sessions too (best-effort — a KC outage never breaks the DB revocation).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import models
from sqlalchemy.orm import Session

from crud import me_auth_apikeys as me_auth_apikeys_crud
# FX.162.p5 — compliance/security-alert services are paid-only (owner Q157):
# the CE carve strips services/account_security_alerts.py, so this module-scope
# import is guarded. `account_security_alerts is None` on CE and every alert
# call site below is skipped (CE fallback = no alert, revocation unchanged).
try:
    from services import account_security_alerts
except ImportError:  # CE carve — module absent, not a bug inside it
    account_security_alerts = None


def revoke_all_other_sessions(db: Session, user: models.User) -> Dict[str, Any]:
    """Revoke every session for `user` except the most-recently-active one.

    Ported 1:1 from the pre-migration router body: a user with 0 or 1
    sessions has nothing to revoke.

    FX.112: the caller passes the `models.User` row (not a bare id) so
    the sign-out can be audited + propagated to Keycloak.
    """
    sessions = me_auth_apikeys_crud.list_sessions_for_user_newest_first(
        db, user.id)

    if len(sessions) <= 1:
        return {"revoked": 0}

    current = sessions[0]
    revoked = 0
    for s in sessions[1:]:
        me_auth_apikeys_crud.delete_session(db, s, commit=False)
        revoked += 1
    db.commit()

    # FX.112 PART 1 — propagate to Keycloak + audit (best-effort).
    if revoked and account_security_alerts is not None:
        kc_ok = account_security_alerts.revoke_keycloak_user_sessions(user)
        account_security_alerts.record_session_sign_out(
            db, user, scope="all",
            details={"revoked_sessions": revoked,
                     "kept_session_id": current.id,
                     "keycloak_revoked": kc_ok})
    return {"revoked": revoked, "kept_session_id": current.id}


def sign_out_everywhere(db: Session, user: models.User,
                        *, initiator_user_id: Optional[int] = None
                        ) -> Dict[str, Any]:
    """FX.112.p4b — sign the user out EVERYWHERE: revoke ALL of the user's
    own session records (including the caller's current one when the user
    is the caller) and all of their Keycloak sessions.

    Unlike ``revoke_all_other_sessions`` (which keeps the most-recent
    session alive), this one revokes every session row. The in-flight
    request is NOT interrupted — the response explicitly tells the client
    it must re-authenticate (``reauth_required: True``); the next request
    with the old token is dead (the session row + KC session are gone).

    ``initiator_user_id`` is set on the tenant-admin variant (a tenant
    admin signing out a user in their own tenant) and lands in the audit
    details; the self-service path leaves it None. Keycloak is
    best-effort (``account_security_alerts.revoke_keycloak_user_sessions``
    — a KC outage never breaks the DB revocation) and the sign-out is
    always audited (SESSION_SIGN_OUT_ALL — audit-only, no email; ISO
    27001 A.9.2.6).
    """
    sessions = me_auth_apikeys_crud.list_sessions_for_user_newest_first(
        db, user.id)

    revoked = 0
    for s in sessions:
        me_auth_apikeys_crud.delete_session(db, s, commit=False)
        revoked += 1
    if revoked:
        db.commit()

    # KC logout-all runs even when no local session row exists (the KC
    # session may live without a mirrored user_sessions row).
    kc_ok = False
    details: Dict[str, Any] = {
        "sign_out_everywhere": True,
        "revoked_sessions": revoked,
        "keycloak_revoked": False,
    }
    if account_security_alerts is not None:
        details["keycloak_revoked"] = bool(
            account_security_alerts.revoke_keycloak_user_sessions(user))
        kc_ok = details["keycloak_revoked"]
        if initiator_user_id is not None:
            details["initiator_user_id"] = initiator_user_id
        # Audit always (the alert helper writes the audit row even on a
        # settings outage; email stays off — the user just did it).
        account_security_alerts.record_session_sign_out(
            db, user, scope="all", details=details)

    return {
        "signed_out": True,
        "revoked_sessions": revoked,
        "keycloak_revoked": kc_ok,
        "reauth_required": True,
    }


def revoke_session(db: Session, user: models.User, session_id: int) -> None:
    """Revoke one session (the DELETE ``/v1/me/sessions/{session_id}``
    endpoint lives in ``routers/me/identity_tenancy.py`` and funnels
    here). FX.112: audits the sign-out and revokes the matching
    Keycloak session (best-effort). Raises ``NotFoundError`` (router
    maps -> 404) if the session doesn't exist or isn't the caller's.
    """
    from crud import me_identity as me_identity_crud
    from services._base import NotFoundError

    row = me_identity_crud.get_session_for_user(db, session_id, user.id)
    if row is None:
        raise NotFoundError("Session not found")
    kc_session_id = row.keycloak_session_id
    kc_ok = (account_security_alerts.revoke_keycloak_session(
        user, kc_session_id) if account_security_alerts is not None else None)
    # ISO-exception: transient UserSession revocation — a session row is an
    # ephemeral auth artifact, not a business/audit record; hard-delete
    # required (revocation must actually remove the credential, and the
    # audit trail lives in AuditEvent, not the session table).
    db.delete(row)
    db.commit()
    if account_security_alerts is not None:
        account_security_alerts.record_session_sign_out(
            db, user, scope="single",
            details={"session_id": session_id,
                     "keycloak_session_id": (kc_session_id or "")[:255] or None,
                     "keycloak_revoked": kc_ok})
