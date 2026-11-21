"""GY.C4.MFA — 2FA handled inside MOD, never on a Keycloak-branded page.

Keycloak still stores and verifies the TOTP secret (no home-grown auth); MOD
only reads status and performs the audited admin reset. Two functions back
the two routes:

    GET  /v1/me/mfa                            -> mfa_status()
    POST /v1/admin/users/{user_id}/mfa/reset   -> reset_user_mfa()

The Keycloak work reuses the shared primitives in services/kc_mfa.py (the
GY.C4 break-glass module). This module adds the status read, the exemption
predicate and the audit + admin-notification fan-out.

Nothing returned here is a secret: credentials are reduced to type /
created_at / device label, and the token used for the admin REST call is
never logged or serialized.
"""
from __future__ import annotations

import logging
import os
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlencode

from sqlalchemy.orm import Session

import models
from services import kc_mfa
from services import platform_settings as psr
from services._base import ConflictError, NotFoundError, ServiceError

logger = logging.getLogger(__name__)

MFA_REQUIRED_FOR_ADMINS_SETTING = "auth.mfa_required_for_admins"
MFA_REQUIRED_FOR_ALL_SETTING = "auth.mfa_required_for_all"

# Same exemption list as CORE/startup/keycloak-init.sh (GSEC R-010): machine
# principals never pass the browser flow, so a queued CONFIGURE_TOTP would
# lock them out for no gain. Kept here so the API reset path cannot drift.
MFA_EXEMPT_GROUP = "e2e_service_principals"
SEED_SERVICE_USERNAMES = ("api@service.local", "platform-service", "api_service")

MFA_RESET_NOTIFICATION_TYPE = "auth_mfa_reset"


class ExemptAccountError(ServiceError):
    """Target is a machine/service principal — 2FA is never queued for it."""

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message, code="conflict", details=details)


def is_exempt_account(username: Optional[str], email: Optional[str],
                      group_names: Optional[Sequence[str]] = None,
                      is_service_account: bool = False) -> bool:
    """True when this account must never get CONFIGURE_TOTP queued.

    Mirrors the shell case statement in keycloak-init.sh: service-account
    users, *@service.local, the seeded api_service / platform-service users,
    and members of the e2e_service_principals exemption group.
    """
    name = (username or "").strip().lower()
    mail = (email or "").strip().lower()
    if name.startswith("service-account-"):
        return True
    if mail.endswith("@service.local") or name.endswith("@service.local"):
        return True
    if name in SEED_SERVICE_USERNAMES:
        return True
    if is_service_account:
        return True
    groups = [str(g).strip().lower() for g in (group_names or [])]
    return MFA_EXEMPT_GROUP in groups


def _setting(key: str, db: Session) -> bool:
    try:
        return bool(psr.get_setting(key, db=db))
    except Exception as exc:  # noqa: BLE001 — setting read must not break status
        logger.warning("mfa_service: setting %s read failed: %s", key, exc)
        return False


def _dashboard_base_url() -> str:
    return os.getenv("DASHBOARD_URL", "http://dashboard.localhost").rstrip("/")


def _kc_action_url(action: str) -> str:
    """MOD-branded Keycloak application-initiated-action URL (modtech login
    theme), returning to the Dashboard.

    PKCE params are NOT included — the Dashboard appends its own code_verifier
    / challenge and its exact redirect_uri (auth.js), so the URL stays valid
    against the confidential dashboard-client.
    """
    from keycloak_auth import KEYCLOAK_EXTERNAL_URL, KEYCLOAK_REALM

    params = {
        "client_id": "dashboard-client",
        "redirect_uri": _dashboard_base_url(),
        "response_type": "code",
        "scope": "openid profile email",
        "kc_action": action,
    }
    return (f"{KEYCLOAK_EXTERNAL_URL.rstrip('/')}/realms/{KEYCLOAK_REALM}"
            f"/protocol/openid-connect/auth?{urlencode(params)}")


def setup_url(db: Session, current_user: models.User) -> str:
    """2FA enrollment URL (CONFIGURE_TOTP on the MOD-themed login page)."""
    return _kc_action_url(kc_mfa.CONFIGURE_TOTP)


def _credential_created_at(value: Any) -> Optional[str]:
    """Keycloak credential `createdDate` normalized to ISO-8601 text.

    The admin REST API returns epoch MILLISECONDS (e.g. 1791162112483), which
    pydantic rejects for the `Optional[str]` response fields — the whole
    /v1/me/mfa read 500s. Epoch ints (Keycloak's unit is ms; a value already
    in the seconds range is read as seconds) become timezone-aware UTC ISO
    text; an already-formatted string or a real datetime is returned as text;
    anything else (None / junk) is None.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else
                value.replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(value, int):
        seconds = value / 1000.0 if abs(value) >= 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        return value
    return None


def _credential_view(creds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Credential rows reduced to non-secret fields (type / label / created)."""
    out = []
    for c in creds:
        if not isinstance(c, dict):
            continue
        out.append({
            "id": c.get("id"),
            "type": c.get("type"),
            "device_label": c.get("userLabel"),
            "created_at": _credential_created_at(c.get("createdDate")),
        })
    return out


def mfa_status(db: Session, current_user: models.User,
              kc_client: Any) -> Dict[str, Any]:
    """GET /v1/me/mfa payload. Never returns a secret."""
    required = bool(current_user.is_platform_admin) and _setting(
        MFA_REQUIRED_FOR_ADMINS_SETTING, db)
    if _setting(MFA_REQUIRED_FOR_ALL_SETTING, db):
        required = True
    exempt = is_exempt_account(
        current_user.username, current_user.email,
        is_service_account=bool(getattr(current_user, "is_service_account", False)))

    payload: Dict[str, Any] = {
        "enrolled": False,
        "credential_created_at": None,
        "device_label": None,
        "required": required,
        "exempt": exempt,
        "setup_url": setup_url(db, current_user),
        "change_password_url": _kc_action_url(kc_mfa.UPDATE_PASSWORD_ACTION),
        # GY.C3b — registered security keys + the MOD-branded register
        # action (Profile > Security "Add security key").
        "webauthn_registered": False,
        "security_keys": [],
        "register_security_key_url":
            _kc_action_url(kc_mfa.WEBAUTHN_REGISTER_ACTION),
        "keycloak_user_id": current_user.keycloak_user_id,
    }
    if exempt:
        return payload

    token = kc_client._get_token()
    base = kc_client.admin_api_url
    realm = _realm_from_admin_url(base)
    session = requests.Session()
    kc_uid = current_user.keycloak_user_id
    if not kc_uid:
        kc_uid = kc_mfa.find_user_id_by_email(
            session, _base_from_admin_url(base), realm, current_user.email, token)
        if not kc_uid:
            return payload
    if not exempt:
        exempt = is_exempt_account(current_user.username, current_user.email,
                                   get_user_group_names(
                                       session, _base_from_admin_url(base), realm,
                                       kc_uid, token),
                                   is_service_account=bool(
                                       getattr(current_user, "is_service_account",
                                               False)))
        payload["exempt"] = exempt
    creds = kc_mfa.get_credentials(session, _base_from_admin_url(base), realm,
                                   kc_uid, token)
    otp = [c for c in creds if kc_mfa.cred_type(c) == kc_mfa.OTP_CREDENTIAL_TYPE]
    if otp:
        # FX.112 PART 1 (Q99=A) — MFA-ADDED security alert, detected here:
        # the status read is the only API-side moment a newly-enrolled
        # credential is visible (enrollment itself happens in Keycloak).
        # Dedupe: once per credential via the SECURITY_ALERT_MFA_ADDED
        # audit row. Best-effort — a failure never changes the payload.
        try:
            # FX.162.p5e — CE carve: no alert module -> no alert (payload
            # unchanged; the status read never depends on the alert).
            from utils.paid_import import optional_paid
            asa = optional_paid("services.account_security_alerts")
            if asa is not None:
                asa.maybe_notify_mfa_added(
                    db, current_user,
                    credential_created_at=_credential_created_at(
                        otp[0].get("createdDate")),
                    credential_id=otp[0].get("id"))
        except Exception as e:  # noqa: BLE001
            logger.warning("FX.112 mfa-added alert failed: %s", e)
        payload["enrolled"] = True
        payload["credential_created_at"] = _credential_created_at(
            otp[0].get("createdDate"))
        payload["device_label"] = otp[0].get("userLabel")
    wa = [c for c in creds
          if kc_mfa.cred_type(c) in (kc_mfa.WEBAUTHN_CREDENTIAL_TYPE,
                                     kc_mfa.WEBAUTHN_PASSWORDLESS_CREDENTIAL_TYPE)]
    if wa:
        payload["webauthn_registered"] = True
        payload["security_keys"] = _credential_view(wa)
    payload["credentials"] = _credential_view(creds)
    return payload


def get_user_group_names(session: requests.Session, base_url: str, realm: str,
                         user_id: str, token: str) -> List[str]:
    """Realm group names for a user (the e2e_service_principals exemption is a
    group membership, so the reset path has to read it). Returns [] on any
    trouble — an unreadable group list never queues an action for a machine
    account, and the username/email predicate still applies."""
    try:
        resp = kc_mfa._request(
            session, "GET", kc_mfa._admin_url(base_url, realm,
                                              f"/users/{user_id}/groups"), token)
        if resp.status_code != 200:
            return []
        groups = resp.json()
        return [g.get("name", "") for g in groups if isinstance(g, dict)]
    except Exception as exc:  # noqa: BLE001 — group read is best-effort
        logger.warning("mfa_service: group read failed for %s: %s", user_id, exc)
        return []


def _realm_from_admin_url(admin_api_url: str) -> str:
    marker = "/admin/realms/"
    if marker in admin_api_url:
        return admin_api_url.split(marker, 1)[1].strip("/")
    return "modtex"


def _base_from_admin_url(admin_api_url: str) -> str:
    marker = "/admin/realms/"
    if marker in admin_api_url:
        return admin_api_url.split(marker, 1)[0]
    return admin_api_url.rstrip("/")


def _notify_platform_admins(db: Session, *, row: models.PermissionAuditLog,
                            target_email: str, actor: str, reason: str) -> int:
    """In-app notification for every active platform admin — the same loud
    pattern services/break_glass.py uses for security events."""
    admins = (
        db.query(models.User)
        .filter(models.User.is_platform_admin.is_(True),
                models.User.status == models.UserStatus.ACTIVE)
        .all()
    )
    if not admins:
        return 0
    message = (
        f"2FA reset: '{actor}' removed the authenticator of {target_email} "
        f"and queued re-enrollment. Reason: {reason}."
    )
    for admin in admins:
        db.add(models.Notification(
            recipient_user_id=admin.id,
            message=message[:1000],
            notification_type=MFA_RESET_NOTIFICATION_TYPE,
            severity="critical",
            is_read=False,
            is_archived=False,
        ))
    return len(admins)


def reset_user_mfa(db: Session, current_user: models.User, user_id: int,
                   reason: str, kc_client: Any, request: Any = None
                   ) -> Dict[str, Any]:
    """Admin 2FA reset: remove the target's OTP and WebAuthn credential(s)
    in Keycloak (GY.C3b — a surviving security key would keep the user
    authenticated after the authenticator was meant to be revoked) and
    queue CONFIGURE_TOTP so they re-enroll on the MOD-branded login page.

    Tenant scoping follows the deactivate/reactivate convention (platform
    admin any tenant, tenant admin own tenant + USER capability). No DB row
    is deleted — the audit row is append-only (ISO 27001).
    """
    from services import users_router_service as users_service

    target = users_service.resolve_target_user_for_admin(
        db, current_user, user_id, "deactivate")
    if target is None:
        raise NotFoundError("User not found")

    token = kc_client._get_token()
    base = kc_client.admin_api_url
    realm = _realm_from_admin_url(base)
    session = requests.Session()
    kc_uid = target.keycloak_user_id or kc_mfa.find_user_id_by_email(
        session, _base_from_admin_url(base), realm, target.email, token)
    if not kc_uid:
        raise NotFoundError("User not found in Keycloak")

    # Refusal happens BEFORE any KC write: an exempt principal must never have
    # CONFIGURE_TOTP queued (a pending required action breaks its
    # client-credentials / direct-grant login — the 10-0x outage cause).
    if is_exempt_account(target.username, target.email,
                         get_user_group_names(session, _base_from_admin_url(base),
                                              realm, kc_uid, token),
                         is_service_account=bool(
                             getattr(target, "is_service_account", False))):
        raise ConflictError(
            "Service accounts are exempt from 2FA — no authenticator is "
            "queued for this account")

    result = kc_mfa.reset_mfa_credentials(
        session, _base_from_admin_url(base), realm, kc_uid, token,
        queue_reenroll=True)

    from audit import log_permission_audit
    row = log_permission_audit(
        db=db,
        action=models.AuditAction.AUTH_MFA_RESET,
        actor_user_id=current_user.id,
        tenant_id=target.tenant_id,
        resource_type="user",
        resource_id=target.id,
        target_user_id=target.id,
        metadata={
            "actor": current_user.email,
            "target_email": target.email,
            "reason": (reason or "")[:500],
            "credentials_removed": result.get("otp_credentials_removed", []),
            "webauthn_credentials_removed":
                result.get("webauthn_credentials_removed", []),
            "reenroll_queued": bool(result.get("reenroll_queued")),
        },
        request=request,
    )
    notified = 0
    if row is not None:
        notified = _notify_platform_admins(
            db, row=row, target_email=target.email,
            actor=current_user.email, reason=(reason or "")[:500])
        db.commit()

    # FX.112 PART 1 (Q99=A) — the affected user must know their 2FA was
    # removed. Best-effort (never blocks the reset).
    try:
        # FX.162.p5e — CE carve: no alert module -> no alert (reset unchanged).
        from utils.paid_import import optional_paid
        asa = optional_paid("services.account_security_alerts")
        if asa is not None:
            asa.notify_mfa_removed(db, target, actor_email=current_user.email)
    except Exception as e:  # noqa: BLE001
        logger.warning("FX.112 mfa-removed alert failed: %s", e)

    return {
        "user_id": target.id,
        "keycloak_user_id": kc_uid,
        "otp_credentials_removed": result.get("otp_credentials_removed", []),
        "webauthn_credentials_removed":
            result.get("webauthn_credentials_removed", []),
        "reenroll_queued": bool(result.get("reenroll_queued")),
        "audit_id": getattr(row, "id", None),
        "notified_admins": notified,
    }


# --------------------------------------------------------------------------- #
# BUG.B5b — admin "Re-link account" (POST /admin/users/{id}/identity/relink)
# --------------------------------------------------------------------------- #
# The 10-08 owner report: Reset 2FA on users 21/22/23 answers 404 because the
# DB row's keycloak_user_id points at no realm user. The admin needs a way to
# rebind the account without guessing. Compliance-safe form (owner Q-B5b=A):
# re-link ONLY when exactly one realm user matches the target's email exactly
# AND that match's email is verified AND it is not already bound to another
# live MOD user; otherwise re-provision a fresh realm account with the repo's
# existing provisioning helper. Every outcome (including refusals) is audited
# with free-form event names on the append-only audit_events table — never a
# token, credential value or password.

ACCOUNT_RELINKED_EVENT = "ACCOUNT_RELINKED"
ACCOUNT_REPROVISIONED_EVENT = "ACCOUNT_REPROVISIONED"
ACCOUNT_RELINK_REFUSED_EVENT = "ACCOUNT_RELINK_REFUSED"


def _resolve_identity_target(db: Session, current_user: models.User,
                             user_id: int) -> "models.User":
    """Same auth gate as the mfa/reset route (platform admin any tenant,
    tenant admin own tenant + USER capability). None -> 404."""
    from services import users_router_service as users_service
    target = users_service.resolve_target_user_for_admin(
        db, current_user, user_id, "deactivate")
    if target is None:
        raise NotFoundError("User not found")
    return target


def _kc_identity_session(kc_client: Any) -> tuple:
    """(token, base_url, realm, session) for the KC admin REST layer."""
    token = kc_client._get_token()
    base = kc_client.admin_api_url
    return token, _base_from_admin_url(base), _realm_from_admin_url(base), requests.Session()


def _kc_user_exists(session, base_url: str, realm: str, kc_uid: str,
                    token: str) -> bool:
    """True when the realm actually has `kc_uid` (404 -> False)."""
    resp = kc_mfa._request(
        session, "GET",
        kc_mfa._admin_url(base_url, realm, f"/users/{kc_uid}"), token)
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    raise kc_mfa._fail("GET", f"/users/{kc_uid}", resp.status_code)


def _kc_email_matches(session, base_url: str, realm: str, email: str,
                      token: str) -> List[Dict[str, Any]]:
    """Exact-email realm lookup (users?email=…&exact=true).

    Returns [{"id", "email_verified"}] — never attributes, credentials or
    tokens. Ambiguity (2+ rows) is what makes a re-link unsafe.
    """
    resp = kc_mfa._request(
        session, "GET",
        kc_mfa._admin_url(base_url, realm, "/users"), token,
        params={"email": email, "exact": "true"})
    if resp.status_code != 200:
        raise kc_mfa._fail("GET", f"/admin/realms/{realm}/users",
                           resp.status_code)
    rows = resp.json()
    if not isinstance(rows, list):
        rows = []
    return [{"id": r.get("id"), "email_verified": bool(r.get("emailVerified"))}
            for r in rows if r.get("id")]


def _kc_bound_to_other_user(db: Session, kc_uid: str,
                            except_user_id: int) -> Optional[int]:
    """MOD user id already holding `kc_uid` (soft-deleted rows ignored)."""
    rows = (db.query(models.User)
            .filter(models.User.keycloak_user_id == kc_uid,
                    models.User.id != except_user_id,
                    models.User.deleted_at.is_(None)).all())
    return rows[0].id if rows else None


def _audit_identity(db: Session, *, event_name: str, target: "models.User",
                    actor: models.User, details: Dict[str, Any]) -> None:
    """Append-only audit row for every relink outcome (refusals included).

    Re-uses the free-form audit_events helper services/data_classification
    .record_audit (the ACCOUNT_* event names are not AuditAction enum members
    and models.py must stay untouched). Never raises, never logs secrets.
    """
    from utils.paid_import import optional_paid
    _dc = optional_paid("services.data_classification")
    if _dc is not None:
        _dc.record_audit(
            db, tenant_id=target.tenant_id, user=actor, event_name=event_name,
            details={"target_user_id": target.id, "actor_email": actor.email,
                     **details})
        return
    # 2400LR-ceimpF2 CE carve: services/data_classification.py is denied (manifest
    # :1037), so its free-form audit helper is absent in CE. These ACCOUNT_* names
    # are not AuditAction enum members (models.py stays untouched), so write the
    # SAME append-only row against the CE-core models.AuditEvent — identical
    # behaviour, still never raises (audit must not drop writes).
    try:
        db.add(models.AuditEvent(
            tenant_id=target.tenant_id,
            user_id=getattr(actor, "id", None),
            event_name=event_name,
            details={"target_user_id": target.id, "actor_email": actor.email,
                     **details}))
        db.commit()
    except Exception:  # noqa: BLE001 — mirrors data_classification.record_audit
        logger.exception("2400LR-ceimpF2: failed to write %s audit row", event_name)


def identity_status(db: Session, current_user: models.User, user_id: int,
                    kc_client: Any) -> Dict[str, Any]:
    """GET /admin/users/{id}/identity/status — what the UI shows the button on.

    {linked, keycloak_user_id, candidate: {email_verified, match_count}}.
    """
    target = _resolve_identity_target(db, current_user, user_id)
    token, base_url, realm, session = _kc_identity_session(kc_client)
    old = target.keycloak_user_id
    linked = False
    if old:
        linked = _kc_user_exists(session, base_url, realm, old, token)
    matches = _kc_email_matches(session, base_url, realm, target.email, token)
    return {
        "linked": bool(linked),
        "keycloak_user_id": old,
        "candidate": {
            "email_verified": bool(matches[0]["email_verified"]) if matches else False,
            "match_count": len(matches),
        },
    }


def relink_identity(db: Session, current_user: models.User, user_id: int,
                    reason: str, confirm: bool, kc_client: Any
                    ) -> Dict[str, Any]:
    """POST /admin/users/{id}/identity/relink (BUG.B5b, owner Q-B5b=A).

    1. current keycloak_user_id resolves in the realm -> ConflictError
       "already linked" (409, no change, audited refusal).
    2. exactly one exact-email realm match, emailVerified true, and that KC
       id bound to no other live MOD user -> rebind (ACCOUNT_RELINKED).
    3. otherwise (no match / unverified / ambiguous / already bound) ->
       re-provision a fresh realm user with the repo's provisioning helper
       (services.external_invitations_service._provision_external_keycloak_user
       — the invite path's KC creator; scim_reconcile._kc_create_user is the
       same shape) and bind it (ACCOUNT_REPROVISIONED).

    `reason` (>=5 chars) and `confirm is True` are required BEFORE any KC
    write; every outcome is audited. No token/password is ever serialized.
    """
    target = _resolve_identity_target(db, current_user, user_id)

    if confirm is not True:
        _audit_identity(db, event_name=ACCOUNT_RELINK_REFUSED_EVENT,
                        target=target, actor=current_user,
                        details={"refusal": "confirm_not_true",
                                 "reason": (reason or "")[:500]})
        raise ServiceError("relink requires confirm=true",
                           code="unprocessable")
    if not (reason or "").strip() or len((reason or "").strip()) < 5:
        _audit_identity(db, event_name=ACCOUNT_RELINK_REFUSED_EVENT,
                        target=target, actor=current_user,
                        details={"refusal": "reason_too_short",
                                 "reason": (reason or "")[:500]})
        raise ServiceError("reason must name why (>=5 characters)",
                           code="unprocessable")

    token, base_url, realm, session = _kc_identity_session(kc_client)
    old = target.keycloak_user_id

    if old and _kc_user_exists(session, base_url, realm, old, token):
        _audit_identity(db, event_name=ACCOUNT_RELINK_REFUSED_EVENT,
                        target=target, actor=current_user,
                        details={"refusal": "already_linked",
                                 "keycloak_user_id": old,
                                 "reason": (reason or "")[:500]})
        raise ConflictError("already linked — the current keycloak_user_id "
                            "resolves in the realm; nothing to re-link")

    matches = _kc_email_matches(session, base_url, realm, target.email, token)
    single = matches[0] if len(matches) == 1 else None
    if (single and single["email_verified"]
            and _kc_bound_to_other_user(db, single["id"], target.id) is None):
        target.keycloak_user_id = single["id"]
        db.commit()
        _audit_identity(db, event_name=ACCOUNT_RELINKED_EVENT,
                        target=target, actor=current_user,
                        details={"old_keycloak_user_id": old,
                                 "new_keycloak_user_id": single["id"],
                                 "matched_by": "exact_email_verified",
                                 "match_count": len(matches),
                                 "reason": (reason or "")[:500]})
        return {"action": "relinked", "user_id": target.id,
                "old_keycloak_user_id": old,
                "new_keycloak_user_id": single["id"],
                "matched_by": "exact_email_verified"}

    if not matches:
        why = "no exact email match in the realm"
    elif len(matches) > 1:
        why = "ambiguous: more than one realm user matches the email"
    elif not single["email_verified"]:
        why = "email match is not verified"
    else:
        why = "realm user already bound to another user"
    new_sub = kc_client.create_user({
        "username": target.email,
        "email": target.email,
        "enabled": True,
        "emailVerified": True,
        "firstName": target.first_name,
        "lastName": target.last_name,
        "requiredActions": ["UPDATE_PASSWORD"],
    })
    target.keycloak_user_id = new_sub
    db.commit()
    try:
        kc_mfa.send_update_password_email(
            session, base_url, realm, new_sub, token)
    except kc_mfa.KcError as exc:  # best-effort — the bind already happened
        logger.warning("identity relink: set-password email failed for %s: %s",
                       new_sub, exc)
    _audit_identity(db, event_name=ACCOUNT_REPROVISIONED_EVENT,
                    target=target, actor=current_user,
                    details={"old_keycloak_user_id": old,
                             "new_keycloak_user_id": new_sub,
                             "reason_for_reprovision": why,
                             "reason": (reason or "")[:500]})
    return {"action": "reprovisioned", "user_id": target.id,
            "old_keycloak_user_id": old, "new_keycloak_user_id": new_sub,
            "reason_for_reprovision": why}
