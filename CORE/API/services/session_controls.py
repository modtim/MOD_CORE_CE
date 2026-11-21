"""GY.C4b — per-user concurrent-session cap via the Keycloak browser flow.

The cap is enforced by Keycloak's user-session-limits authenticator
(provider id ``user-session-limits``, org.keycloak.keycloak-services —
``UserSessionLimitsAuthenticatorFactory``), installed as an execution in
the realm's ``browser`` authentication flow. ``services/platform_settings.py`` calls
:func:`converge_session_limiter` (deferred to after the settings commit,
exactly like the AC-8 banner and GY.C3b WebAuthn browser-flow syncs)
whenever ``auth.session_max_concurrent`` or
``auth.session_limit_behaviour`` changes, so a change takes effect without
a Keycloak restart.

Design notes
------------
* No home-grown auth: the LIMIT itself lives in Keycloak. This module only
  configures the authenticator through the admin REST API (the same client
  and access pattern as ``services/webauthn_policy.py`` /
  ``services/kc_mfa.py``). Service accounts, client-credentials clients and
  e2e principals never traverse the browser flow, so they are exempt by
  construction.
* Idempotent: GET the browser flow, find the existing execution, and only
  POST/PUT when the shape or config actually differs. Re-applying is a
  no-op.
* ``auth.session_max_concurrent == 0`` (default) leaves the authenticator
  DISABLED — unlimited, exactly today's behaviour.
* Provider-absent tolerance (defensive): if the authenticator is somehow
  not present (older builds / custom realms), adding the execution is
  refused by Keycloak; we catch that, record it and RETURN (never raising,
  never failing the settings write). The provider id and config keys
  below match Keycloak 26.3.5 (``user-session-limits`` with
  ``userRealmLimit`` / ``userClientLimit`` / ``behavior``); they are
  isolated as constants so they stay trivial to adjust. ``keycloak-init.sh``
  converges the same shape on every boot so a restart re-applies the
  intent.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests
from sqlalchemy.orm import Session

try:
    from services import kc_mfa
except ImportError:  # CE carve — module absent
    kc_mfa = None
    class _KcMfaGuardError(Exception):  # CE stand-in for kc_mfa.KcError
        pass

_KcMfaKcError = kc_mfa.KcError if kc_mfa is not None else _KcMfaGuardError

from services import platform_settings as psr

logger = logging.getLogger(__name__)

# Keycloak user-session-limits authenticator (26.3.5,
# UserSessionLimitsAuthenticatorFactory). Isolated constants — if a future
# Keycloak build renames these, adjust here and in keycloak-init.sh only.
SESSION_LIMIT_PROVIDER_ID = "user-session-limits"
CONFIG_USER_REALM_LIMIT = "userRealmLimit"
CONFIG_USER_CLIENT_LIMIT = "userClientLimit"
CONFIG_BEHAVIOR = "behavior"
# The authenticator's behaviour property takes these exact display strings
# (its admin UI renders them verbatim).
_BEHAVIOR_VALUES = {
    "deny_new": "Deny new session",
    "terminate_oldest": "Terminate oldest session",
}

# Browser-flow execution requirement values (Keycloak ExecutionStatus).
_REQ_ENABLED = "ENABLED"
_REQ_DISABLED = "DISABLED"


def _base_from_admin_url(admin_api_url: str) -> str:
    return admin_api_url.split("/admin/", 1)[0]


def _realm_from_admin_url(admin_api_url: str) -> str:
    parts = admin_api_url.split("/")
    return parts[-1]


def _get_browser_flow(session: requests.Session, base: str, realm: str,
                      token: str) -> Dict[str, Any]:
    if kc_mfa is None:
        raise _KcMfaKcError("kc_mfa unavailable in this installation")
    resp = kc_mfa._request(session, "GET",
                           kc_mfa._admin_url(base, realm, "/authentication/flows"),
                           token)
    if resp.status_code != 200:
        raise kc_mfa.KcError("GET", "/authentication/flows", resp.status_code)
    for flow in resp.json():
        if isinstance(flow, dict) and flow.get("alias") == "browser":
            return flow
    raise kc_mfa.KcError("GET", "/authentication/flows(browser)", 404)


def _find_execution(flow: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The execution carrying our provider, among the browser flow's steps."""
    for step in (flow.get("authenticationFlows") or []):
        if not isinstance(step, dict):
            continue
        if step.get("providerId") == SESSION_LIMIT_PROVIDER_ID:
            return step
    return None


def _add_execution(session: requests.Session, base: str, realm: str,
                   token: str, flow_id: str, requirement: str) -> Optional[str]:
    """POST a new execution under the browser flow. Returns the execution id
    (parsed from the Location header) or None when Keycloak refuses it —
    which is the provider-absent case (400) and is NOT an error here."""
    if kc_mfa is None:
        raise _KcMfaKcError("kc_mfa unavailable in this installation")
    resp = kc_mfa._request(
        session, "POST",
        kc_mfa._admin_url(base, realm, f"/authentication/executions/flow/{flow_id}"),
        token,
        json={"providerId": SESSION_LIMIT_PROVIDER_ID,
              "authenticationFlow": False,
              "requirement": requirement})
    if resp.status_code in (200, 201, 204):
        loc = resp.headers.get("Location") or ""
        return loc.rsplit("/", 1)[-1] or None
    # 400 (unknown provider) or 409 (already present) -> not a hard failure.
    logger.info("session_controls: add-execution %s status=%s",
                SESSION_LIMIT_PROVIDER_ID, resp.status_code)
    return None


def _set_config(session: requests.Session, base: str, realm: str,
                token: str, execution_id: str, config: Dict[str, Any]) -> bool:
    if kc_mfa is None:
        raise _KcMfaKcError("kc_mfa unavailable in this installation")
    resp = kc_mfa._request(
        session, "PUT",
        kc_mfa._admin_url(base, realm, f"/authentication/config/{execution_id}"),
        token, json=config)
    return resp.status_code in (200, 204)


def _set_requirement(session: requests.Session, base: str, realm: str,
                     token: str, execution_id: str, requirement: str) -> bool:
    if kc_mfa is None:
        raise _KcMfaKcError("kc_mfa unavailable in this installation")
    resp = kc_mfa._request(
        session, "PUT",
        kc_mfa._admin_url(base, realm, f"/authentication/executions/{execution_id}"),
        token, json={"id": execution_id, "requirement": requirement,
                     "priority": None, "providerId": SESSION_LIMIT_PROVIDER_ID,
                     "authenticationFlow": False})
    return resp.status_code in (200, 204)


def converge_session_limiter(db: Session, kc_client: Any) -> Dict[str, Any]:
    """Converge the browser-flow session limiter on the two live settings.

    Returns a small dict for the caller to log/audit:
    ``{"enabled": bool, "max": int, "behaviour": str, "provider_present":
    Optional[bool], "applied": bool, "note": str}``. Never raises — a
    Keycloak failure is reported in ``note`` and left to keycloak-init.sh
    to re-converge (same contract as the AC-8 banner + GY.C3b syncs).
    """
    if kc_mfa is None:
        return {"enabled": False, "max": 0, "behaviour": "deny_new",
                "provider_present": None, "applied": False,
                "note": "kc_mfa unavailable in this installation"}
    result: Dict[str, Any] = {
        "enabled": False, "max": 0, "behaviour": "deny_new",
        "provider_present": None, "applied": False, "note": ""}

    try:
        max_concurrent = int(psr.get_setting(
            psr.SESSION_LIMIT_SETTING_KEY, db=db) or 0)
        behaviour = str(psr.get_setting(
            psr.SESSION_LIMIT_BEHAVIOUR_SETTING_KEY, db=db) or "deny_new")
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"setting read failed: {exc}"
        return result
    result.update(max=max_concurrent, behaviour=behaviour)

    try:
        token = kc_client._get_token()
        base = _base_from_admin_url(kc_client.admin_api_url)
        realm = _realm_from_admin_url(kc_client.admin_api_url)
        sess = requests.Session()
        flow = _get_browser_flow(sess, base, realm, token)
        step = _find_execution(flow)

        if max_concurrent <= 0:
            # Unlimited: leave the authenticator disabled (or absent).
            if step is not None:
                _set_requirement(sess, base, realm, token,
                                 step.get("id"), _REQ_DISABLED)
                result.update(applied=True, provider_present=True,
                              note="disabled (max=0)")
            else:
                result.update(provider_present=False, note="max=0, absent")
            return result

        # max > 0 — ensure the execution exists and is ENABLED.
        if step is None:
            exec_id = _add_execution(sess, base, realm, token,
                                     flow.get("id"), _REQ_ENABLED)
            if not exec_id:
                # Re-read: the POST may have been refused (provider absent
                # in this Keycloak build) or the step now exists.
                flow2 = _get_browser_flow(sess, base, realm, token)
                step2 = _find_execution(flow2)
                if step2 is None:
                    result.update(provider_present=False,
                                  note="provider not present in this "
                                       "Keycloak build; setting not "
                                       "applied (re-converges on next "
                                       "change or keycloak-init.sh)")
                    return result
                step = step2
        if step.get("requirement") != _REQ_ENABLED:
            _set_requirement(sess, base, realm, token, step.get("id"),
                             _REQ_ENABLED)

        # userClientLimit 0 = no separate per-client cap; the per-user
        # realm-wide cap (userRealmLimit) is what the setting means here.
        config = {CONFIG_USER_REALM_LIMIT: max_concurrent,
                  CONFIG_USER_CLIENT_LIMIT: 0,
                  CONFIG_BEHAVIOR: _BEHAVIOR_VALUES.get(
                      behaviour, "Deny new session")}
        _set_config(sess, base, realm, token, step.get("id"), config)
        result.update(applied=True, provider_present=True,
                      note=f"max={max_concurrent} behaviour={behaviour}")
        return result
    except _KcMfaKcError as exc:
        result["note"] = f"kc error: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"{type(exc).__name__}: {exc}"
        return result
