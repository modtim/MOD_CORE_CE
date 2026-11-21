"""GY.C4 — reusable Keycloak MFA primitives (2FA reset / re-enrollment).

The break-glass 2FA reset (CORE/startup/break-glass.sh, host side) and the
GY.C4.MFA admin "Reset 2FA" endpoint share these primitives: look up a
realm user by email, remove that user's OTP and WebAuthn credential(s)
via the KC admin REST API, re-queue the CONFIGURE_TOTP required action
(so the user re-enrolls on next login — the realm's stock browser flow
already carries the conditional-OTP step; GY.C3b adds the WebAuthn
alternative next to it), and optionally send the KC
execute-actions email that forces a password update.

The caller passes the admin bearer token (obtained from the admin-cli
client) and is responsible for never logging it. Errors raise KcError
with a status code and the offending path — never the token or any
credential value.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
CONFIGURE_TOTP = "CONFIGURE_TOTP"
OTP_CREDENTIAL_TYPE = "otp"
UPDATE_PASSWORD_ACTION = "UPDATE_PASSWORD"
# GY.C3b — WebAuthn second factor (phishing-resistant). The credential type
# Keycloak stores registered security keys under, and the required-action
# aliases keycloak-init.sh converges (registration from the MOD
# Profile > Security page; authentication on the browser login flow).
WEBAUTHN_CREDENTIAL_TYPE = "webauthn"
#: GY.C3b — WebAuthn second factor (phishing-resistant). The credential type
# Keycloak stores registered security keys under, and the required-action
# aliases keycloak-init.sh converges (registration from the MOD
# Profile > Security page; authentication on the browser login flow).
# KC 26.3.5 stores credential `type` LOWERCASE (verified against the realm
# jars: OTPCredentialProvider -> "otp", WebAuthnCredentialProvider ->
# "webauthn", WebAuthnPasswordlessCredentialProvider ->
# "webauthn-passwordless", PasswordCredentialProvider -> "password"), so
# every comparison below goes through cred_type() rather than matching the
# raw JSONB field case-sensitively (BUG 1790: the uppercase constants made
# enrolled_factor() blind to real OTP, so the MFA sweep re-queued
# CONFIGURE_TOTP on every login).
WEBAUTHN_PASSWORDLESS_CREDENTIAL_TYPE = "webauthn-passwordless"
WEBAUTHN_REGISTER_ACTION = "WEBAUTHN_REGISTER"
WEBAUTHN_AUTHENTICATE_ACTION = "WEBAUTHN_AUTHENTICATE"
#: Credential types that count as an enrolled second factor. Passwordless
# WebAuthn (passkeys) is a real registered factor, so it counts too.
ENROLLED_CREDENTIAL_TYPES = (
    OTP_CREDENTIAL_TYPE,
    WEBAUTHN_CREDENTIAL_TYPE,
    WEBAUTHN_PASSWORDLESS_CREDENTIAL_TYPE,
)


class KcError(RuntimeError):
    """A KC admin REST call returned an unexpected status. The message
    carries the status code and path only — no headers, no secrets."""

    def __init__(self, method: str, path: str, status: int):
        super().__init__(f"{method} {path} -> HTTP {status}")
        self.method = method
        self.path = path
        self.status = status


class KcUserMissing(KcError):
    """The realm has no such user (GET /users/{id} -> 404).

    Not a provider outage — the DB<->Keycloak binding is stale (the realm
    user was deleted, or never provisioned). Callers map this to "user not
    found in Keycloak" (404), never to a 502 that tells the admin nothing.
    """

    def __init__(self, user_id: str):
        super().__init__("GET", f"/users/{user_id}", 404)
        self.user_id = user_id


def cred_type(cred: Any) -> str:
    """Normalized credential `type` (BUG 1790).

    KC stores it lowercase, but every consumer used to compare it
    case-sensitively against uppercase constants; normalizing here means a
    future realm/constant casing change can never silently blind a factor
    check again. Non-dict rows normalize to ""."""
    if not isinstance(cred, dict):
        return ""
    return (cred.get("type") or "").lower()


def _admin_url(base_url: str, realm: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/admin/realms/{realm}{path}"


def _bearer(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _fail(method: str, path: str, status: int) -> "KcError":
    """Build a KcError AND log method/path/status (BUG.B5: the admin reset
    used to answer a blind 502 — the real KC status is what makes the next
    report diagnosable). Never logs headers, bodies or tokens."""
    logger.warning("kc_mfa: %s %s -> HTTP %s", method, path, status)
    return KcError(method, path, status)


def _request(session: requests.Session, method: str, url: str,
             token: str, **kwargs) -> requests.Response:
    resp = session.request(
        method, url, headers=_bearer(token),
        timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT), **kwargs)
    return resp


def find_user_id_by_email(session: requests.Session, base_url: str,
                          realm: str, email: str, token: str
                          ) -> Optional[str]:
    """Exact-match realm user lookup by email. None when absent."""
    resp = _request(
        session, "GET", _admin_url(base_url, realm, "/users"), token,
        params={"email": email, "exact": "true"})
    if resp.status_code != 200:
        raise _fail("GET", f"/admin/realms/{realm}/users", resp.status_code)
    users = resp.json()
    if not users:
        return None
    return users[0].get("id")


def get_credentials(session: requests.Session, base_url: str, realm: str,
                    user_id: str, token: str) -> List[Dict[str, Any]]:
    resp = _request(
        session, "GET",
        _admin_url(base_url, realm, f"/users/{user_id}/credentials"), token)
    if resp.status_code == 404:
        # The realm has no such user — the DB<->KC binding is stale (the
        # realm user was deleted / never provisioned). Distinct from a
        # provider outage so the caller can say "not found in Keycloak"
        # instead of a blind 502 (BUG.B5).
        raise KcUserMissing(user_id)
    if resp.status_code != 200:
        raise _fail("GET", f"/users/{user_id}/credentials",
                    resp.status_code)
    creds = resp.json()
    return creds if isinstance(creds, list) else []


def reset_mfa_credentials(session: requests.Session, base_url: str,
                          realm: str, user_id: str, token: str,
                          *, queue_reenroll: bool = True) -> Dict[str, Any]:
    """Remove the user's OTP AND WebAuthn credential(s), re-queue
    CONFIGURE_TOTP.

    This is the standard KC credential reset: the deleted rows are KC
    credentials (not tenant data). GY.C3b: a 2FA reset is a full 2FA
    reset — a surviving security key would keep the user authenticated
    after the authenticator was meant to be revoked. Returns
    ``{"otp_credentials_removed": [id, ...],
       "webauthn_credentials_removed": [id, ...], "reenroll_queued": bool}``.
    A user with no credentials still gets the re-enrollment queue
    (idempotent — an already-queued action is not duplicated).
    """
    creds = get_credentials(session, base_url, realm, user_id, token)
    otp_ids = [c.get("id") for c in creds
               if cred_type(c) == OTP_CREDENTIAL_TYPE and c.get("id")]
    # A passkey (webauthn-passwordless) is a surviving second factor just
    # like a registered security key — a 2FA reset has to remove it too.
    webauthn_ids = [c.get("id") for c in creds
                    if cred_type(c) in (WEBAUTHN_CREDENTIAL_TYPE,
                                        WEBAUTHN_PASSWORDLESS_CREDENTIAL_TYPE)
                    and c.get("id")]
    for cred_id in otp_ids + webauthn_ids:
        resp = _request(
            session, "DELETE",
            _admin_url(base_url, realm,
                       f"/users/{user_id}/credentials/{cred_id}"), token)
        # 404 = the credential is already gone (double-click / two admins
        # reset at once) — the reset is idempotent, not a failure.
        if resp.status_code not in (204, 404):
            raise _fail("DELETE",
                        f"/users/{user_id}/credentials/{cred_id}",
                        resp.status_code)

    reenroll_queued = False
    if queue_reenroll:
        resp = _request(session, "GET",
                        _admin_url(base_url, realm, f"/users/{user_id}"),
                        token)
        if resp.status_code == 404:
            raise KcUserMissing(user_id)
        if resp.status_code != 200:
            raise _fail("GET", f"/users/{user_id}", resp.status_code)
        user = resp.json()
        actions = list(user.get("requiredActions") or [])
        if CONFIGURE_TOTP not in actions:
            actions.append(CONFIGURE_TOTP)
            put = _request(
                session, "PUT", _admin_url(base_url, realm, f"/users/{user_id}"),
                token, json={"requiredActions": actions})
            if put.status_code not in (200, 204):
                raise _fail("PUT", f"/users/{user_id}", put.status_code)
            reenroll_queued = True
    return {
        "otp_credentials_removed": otp_ids,
        "webauthn_credentials_removed": webauthn_ids,
        "reenroll_queued": reenroll_queued,
    }


def send_update_password_email(session: requests.Session, base_url: str,
                               realm: str, user_id: str, token: str,
                               *, lifespan_seconds: int = 3600) -> None:
    """Send the KC execute-actions email that forces a password update on
    next login (break-glass --reset-password path: the admin is walked
    through a reset instead of being handed a new password)."""
    resp = _request(
        session, "POST",
        _admin_url(base_url, realm, f"/users/{user_id}/execute-actions-email"),
        token,
        json={"action": UPDATE_PASSWORD_ACTION,
              "lifespan": int(lifespan_seconds)})
    if resp.status_code != 204:
        raise _fail("POST", f"/users/{user_id}/execute-actions-email",
                    resp.status_code)
