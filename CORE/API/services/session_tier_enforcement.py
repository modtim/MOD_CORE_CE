"""GSEC FX.54 part 2 (owner Q37=A + Q49=A) — server-side ADMIN-tier session limits.

Keycloak's realm carries ONE idle/max pair, so it carries the most permissive
tier it serves (the human-USER tier: 60 min idle / 12 h max). The admin tier
(30 min idle / 8 h max, owner Q37=A) cannot be a realm value, so it is enforced
here, on the API auth path, for every interactive human request:

    max  — now - <token auth_time>  > auth.admin_sso_session_max_lifetime_seconds
    idle — no activity for           > auth.admin_sso_session_idle_seconds

FX.54 p3b: that comparison is NOT done here any more — the verdict and the
tier numbers come from services/session_policy.evaluate_session_policy (p3a),
so the auth path, the /me dialog, the remember-me cut (p3c) and the KC realm
push (p3d) all read one source of truth. This module keeps the effectful half
only: the per-sid activity stamps, the audit row and the 401.

Activity is tracked per Keycloak SESSION id (the `sid` claim), which is the
identity Keycloak itself expires — so a warning/401 here matches the session the
browser actually holds.

Scope (by construction):
  * Only INTERACTIVE human user tokens are checked. API-key / service-account
    tokens have no `auth_time` and no `sid`, and the caller in
        `dependencies.get_current_user` only invokes the gate on the human
        branch — so machine traffic never reaches it.
  * FX.54 p3c: the gate now runs for EVERY interactive human token, each against
        ITS OWN tier (platform_admin / tenant_admin / normal via p3a's tier_for),
        and passes the session's real remember_me value — the ``offline_access``
        scope claim (Keycloak has no remember_me claim; the opt-in IS that scope,
        and the realm grants it only while rememberMe is on). Turning the toggle
        off or shortening a pair therefore CUTS an already-long session on its
        next request instead of letting it survive until re-login. Admins keep
        their tighter pair; the normal tier is bounded by the user pair + the
        remember-me trio exactly as the realm was configured to do.
  * Fail-OPEN when the settings read fails (logged), matching the GSEC R-083 /
        GY.C4b posture: a settings or cache failure must not lock operators out.
  * Rejections write an audit/security event (AU-2) before the 401.

Idle-activity store — documented limitation
-------------------------------------------
Last-activity per `sid` lives in Valkey/Redis (the general cache, same
`REDIS_URL` seam as utils.rate_limiter), keyed `fx54:session_activity:<sid>`
with a TTL equal to the widest tier so a key can never outlive its session.
When Redis is unreachable the gate degrades to a bounded IN-PROCESS map, which
is correct only for the process that wrote it: with more than one API worker a
request that lands on a different worker sees no activity for that session and
is treated as idle-expired (fail-CLOSED for that request only). Redis is the
single source of truth for the tier; the in-process map is the degraded path,
never the primary one.

The 401 detail is structured (`error: "session_expired"`, `session_tier`,
`idle_seconds`, `max_lifetime_seconds`) so the Dashboard/WB idle-warning dialog
and the api.js handler can read the numbers without parsing prose.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

import audit
import httpx
import models
from services import platform_settings as psr
# FX.54 p3a — the pure decision function. p2 keeps the effectful half (401 +
# audit + activity stamps); the NUMBERS and the verdict come from p3a so the
# auth path, the /me dialog, the remember-me cut and the KC push all read one
# source of truth. No cycle: session_policy imports platform_settings only,
# and platform_settings reaches THIS module lazily, inside a function.
import services.session_policy as session_policy

logger = logging.getLogger(__name__)

ADMIN_IDLE_SETTING_KEY = "auth.admin_sso_session_idle_seconds"
ADMIN_MAX_SETTING_KEY = "auth.admin_sso_session_max_lifetime_seconds"

# Session tiers the dialog can read from /me (FX.54 part 2).
USER_TIER_SETTING_KEYS = {
    "idle": "auth.sso_session_idle_seconds",
    "max": "auth.sso_session_max_lifetime_seconds",
}

# FX.54 p3c — the per-session remember-me marker. Keycloak has no "remember_me"
# claim; the opt-in IS the offline_access scope (granted only while the realm's
# rememberMe is on, and the Dashboard/WB login URLs ask for it).
OFFLINE_ACCESS_SCOPE = "offline_access"

# The 401 + /me wire names (FX.54 part 2's "admin"/"user") for p3a's canonical
# tier names, so the dialog/api.js handler keep reading the same vocabulary.
TIER_WIRE_NAMES = {session_policy.PLATFORM_ADMIN_TIER: "admin",
                   session_policy.TENANT_ADMIN_TIER: "tenant_admin",
                   session_policy.NORMAL_TIER: "user"}

ACTIVITY_KEY_PREFIX = "fx54:session_activity:"
# TTL = the widest tier Keycloak can hand out (remember-me, 7 d), so an
# activity key can never outlive the session it describes.
ACTIVITY_TTL_SECONDS = 604800

# Degraded in-process store (see the module docstring): bounded so a long-lived
# API process cannot grow it without limit; oldest entries are dropped first.
_FALLBACK_ACTIVITY: Dict[str, float] = {}
_FALLBACK_MAX_ENTRIES = 5000

_redis_client: Any = None
_redis_url: Optional[str] = None


def _get_redis():
    """Lazy Valkey/Redis connection (same seam as utils.rate_limiter). Returns
    None when the module or the server is unavailable — the caller then uses the
    in-process fallback map."""
    global _redis_client, _redis_url
    url = os.getenv("REDIS_URL", "redis://redis:6379/2")
    if _redis_client is not None and _redis_url == url:
        return _redis_client
    try:
        import redis  # local import — pytest can pre-mock the module
        _redis_client = redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=2,
            socket_timeout=2)
        _redis_url = url
        return _redis_client
    except Exception as exc:  # noqa: BLE001 — cache failure is not a request failure
        logger.warning("FX.54: Redis unavailable for session-activity tracking "
                       "(in-process fallback only): %s", exc)
        _redis_client = None
        _redis_url = None
        return None


def _remember(key: str, now: float) -> None:
    if len(_FALLBACK_ACTIVITY) >= _FALLBACK_MAX_ENTRIES:
        # Drop the oldest entries before inserting (bounded map, no unbounded growth).
        for stale in sorted(_FALLBACK_ACTIVITY, key=_FALLBACK_ACTIVITY.get)[:
                _FALLBACK_MAX_ENTRIES // 2]:
            _FALLBACK_ACTIVITY.pop(stale, None)
    _FALLBACK_ACTIVITY[key] = now


def _record_activity(sid: str, db: Session, user: models.User,
                     request: Optional[Request]) -> None:
    key = ACTIVITY_KEY_PREFIX + sid
    now = time.time()
    r = _get_redis()
    if r is not None:
        try:
            r.set(key, now, ex=ACTIVITY_TTL_SECONDS)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("FX.54: session-activity write failed (fallback map): "
                           "%s", exc)
    _remember(key, now)


def _last_activity(key: str) -> Optional[float]:
    r = _get_redis()
    if r is not None:
        try:
            raw = r.get(key)
            if raw is not None:
                return float(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("FX.54: session-activity read failed (fallback map): "
                           "%s", exc)
    return _FALLBACK_ACTIVITY.get(key)


def _remember_me_for_claims(claims: Dict[str, Any], db: Session) -> bool:
    """FX.54 p3c — is THIS session a remember-me (long) session?

    The per-session opt-in is the ``offline_access`` scope: the Dashboard/WB
    login URLs ask for it (CORE/Dashboard/js/auth.js:113/176, and the same
    scope on the WB + app clients), Keycloak grants it only while the realm's
    ``rememberMe`` is on, and the access token carries the granted scopes in
    its ``scope`` claim. So the claim — not the current setting — is what says
    the USER asked to stay signed in; the CURRENT setting is applied by p3a's
    evaluator, which is exactly what makes a toggle-off cut an already-long
    session on its next request instead of letting it survive to re-login.

    With no ``scope`` claim (a client that does not carry one) fall back to the
    realm toggle, i.e. assume the opt-in the realm is currently offering.
    Never raises: a settings read failure resolves to False (no extension).
    """
    scope = claims.get("scope")
    if isinstance(scope, str):
        return OFFLINE_ACCESS_SCOPE in scope.split()
    if scope is None:
        try:
            return bool(psr.get_setting(
                session_policy.REMEMBER_ME_SETTING_KEYS["enabled"], db=db))
        except Exception as exc:  # noqa: BLE001 — fail-open: no extension
            logger.warning("FX.54 p3c: remember-me toggle read failed "
                           "(no extension): %s", exc)
            return False
    return False


def _policy_verdict(db: Session, user_tier: Any, auth_time: Any,
                    last_activity_at: Optional[float], now: float,
                    remember_me: bool = False) -> Dict[str, Any]:
    """FX.54 p3b — the DECISION comes from services/session_policy.

    p3a's evaluator is the single source of truth for the tier numbers and the
    verdict; this module keeps only the effectful half (redis stamps, audit row,
    HTTP 401). ``last_activity_at`` is the prior sighting of this ``sid`` (None
    = first sighting); the login itself counts as activity, so the idle window
    starts now and idle can never reject a first sighting — p2's rule, fed to
    the evaluator rather than re-implemented here.

    Never raises: the evaluator is total, and a settings read that blows up
    resolves to limit=None (off) + ``settings_errors`` (logged by p3a), i.e.
    ALLOW = fail-open, matching the GSEC R-083 posture. The try/except below
    is the belt-and-braces half: if the evaluator itself ever blows up, the
    request is ALLOWED and the failure is logged, never 500'd and never
    silently treated as "expired".
    """
    try:
        return session_policy.evaluate_session_policy(
            user_tier, auth_time,
            last_activity_at if last_activity_at is not None else now,
            remember_me=remember_me, now=now, db=db)
    except Exception as exc:  # noqa: BLE001 — evaluator failure = fail-open
        logger.warning("FX.54 p3b: session policy evaluator failed (fail-open "
                       "ALLOW): %s", exc)
        return {"decision": session_policy.ALLOW, "tier": "unknown",
                "idle_seconds": None, "absolute_seconds": None,
                "effective_idle_seconds": None,
                "effective_absolute_seconds": None,
                "age_seconds": None, "idle_elapsed_seconds": None,
                "seconds_over": 0, "remember_me_applied": False,
                "settings_errors": ["evaluator"]}


def _reject(db: Session, request: Optional[Request], user: models.User,
           claims: Dict[str, Any], reason: str, idle: Optional[int],
           max_life: Optional[int], seconds_over: float,
           tier: str = session_policy.PLATFORM_ADMIN_TIER,
           remember_me: bool = False) -> HTTPException:
    """FX.54 p3c: the tier + the remember-me flag come from the verdict, so a
    normal-tier remember-me cut reports itself (``session_tier: "normal"``,
    ``remember_me: true``) exactly like the admin pair always has."""
    wire_tier = TIER_WIRE_NAMES.get(tier, tier)
    detail = {
        "error": "session_expired",
        "session_tier": wire_tier,
        "remember_me": remember_me,
        "idle_seconds": idle,
        "max_lifetime_seconds": max_life,
        "detail": (
            f"Session expired ({tier} tier): "
            + ("no activity for " if reason == "idle" else "login age ")
            + f"{int(seconds_over)} s exceeds the {idle if reason == 'idle' else max_life} s "
            + ("idle" if reason == "idle" else "max-lifetime")
            + f" limit for {tier} tier. Sign in again."
        ),
    }
    try:
        audit.log_security_event(
            db,
            models.AuditAction.AUTH_TOKEN_EXPIRED,
            request=request,
            user_id=user.id,
            tenant_id=user.tenant_id,
            metadata={"event": "auth.session_tier.expired",
                      "reason": reason,
                      "session_id": claims.get("sid"),
                      "tier": tier,
                      "remember_me": remember_me,
                      "idle_seconds": idle,
                      "max_lifetime_seconds": max_life,
                      "seconds_over": int(seconds_over)},
        )
    except Exception as exc:  # noqa: BLE001 — the audit row must not mask the 401
        logger.warning("FX.54: audit emit failed for admin-tier session expiry: %s",
                       exc)
    logger.warning("FX.54: admin-tier session %s for user=%s (sid=%s)",
                   reason, user.id, claims.get("sid"))
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def enforce_admin_session_tier(db: Session, user: models.User,
                               claims: Dict[str, Any],
                               request: Optional[Request] = None) -> models.User:
    """FX.54 part 2 — admin-tier max-lifetime + idle check on an interactive
    human token (see the module docstring). Returns the user when the session is
    inside both limits; raises 401 (with an audit event) when it is not.

    Called from `dependencies.get_current_user` on the human branch only, so
    API-key / service-account tokens never reach it.

    FX.54 p3b: the verdict + the tier numbers come from
    ``services.session_policy.evaluate_session_policy`` (p3a) — this function
    keeps only the effectful half: the per-``sid`` activity stamps, the audit
    row and the 401. The stamps still gate the IDLE leg (the evaluator has no
    redis: p3a is pure, so the caller feeds it the prior sighting), which is
    what keeps a first sighting from ever rejecting on idle.

    FX.54 p3c: the gate is NO LONGER admin-only. It runs for every interactive
    human token against ITS OWN tier (platform_admin / tenant_admin / normal,
    resolved by p3a's ``tier_for`` from ``is_platform_admin``), and it passes
    the session's real remember_me value (the ``offline_access`` scope claim,
    see ``_remember_me_for_claims``) instead of False. That is what makes the
    live cut work: a remember-me session whose age exceeds the CURRENT limits
    401s on the very next request the moment the toggle is turned off or the
    pair is shortened, because the evaluator reads the registry per call and
    never sees the stale opt-in as an extension."""
    auth_time = claims.get("auth_time")
    sid = claims.get("sid")
    if auth_time is None and sid is None:
        # No human login context (machine token): nothing to bound here.
        return user

    now = time.time()
    key = ACTIVITY_KEY_PREFIX + sid if sid else None
    last = _last_activity(key) if key else None
    remember_me = _remember_me_for_claims(claims, db)

    verdict = _policy_verdict(db, bool(getattr(user, "is_platform_admin", False)),
                              auth_time, last, now, remember_me)
    tier = verdict["tier"]
    idle = verdict["effective_idle_seconds"]
    max_life = verdict["effective_absolute_seconds"]
    if verdict["settings_errors"]:
        logger.warning("FX.54 p3b: %s-tier session policy had setting errors "
                       "(fail-open for those limits): %s", tier,
                       verdict["settings_errors"])

    if verdict["decision"] == session_policy.REJECT_ABSOLUTE:
        raise _reject(db, request, user, claims, "max", idle, max_life,
                      verdict["seconds_over"], tier=tier,
                      remember_me=verdict["remember_me_applied"])
    if verdict["decision"] == session_policy.REJECT_IDLE:
        # No stamp on a rejection (p2's rule): the session stays past its idle
        # limit until the user actually re-authenticates, so the next request
        # 401s again rather than silently getting a fresh window.
        raise _reject(db, request, user, claims, "idle", idle, max_life,
                      verdict["seconds_over"], tier=tier,
                      remember_me=verdict["remember_me_applied"])

    if key:
        # ALLOW: stamp the sighting (first sighting or inside the window).
        _record_activity(sid, db, user, request)

    return user


# ---------------------------------------------------------------------------
# Live realm sync (FX.54 part 2)
# ---------------------------------------------------------------------------
# The realm fields the registry drives: the user-tier pair (the realm carries
# the most permissive tier it serves) and the remember-me trio. The ADMIN tier
# is not a realm field — it lives in the enforcement path above — so admin-tier
# setting changes do NOT touch the realm.
#
# Same seam as services/lockout_policy.converge_lockout: PARTIAL realm PUT that
# carries the live fields through (Keycloak 26.x re-applies its own defaults for
# omitted fields — omitting `attributes` is what wiped frontendUrl in the
# 2026-10-04 login outage), never raises, and leaves a failure to
# keycloak-init.sh to re-converge on boot.
REALM_SESSION_TIER_FIELDS = ("ssoSessionIdleTimeout", "ssoSessionMaxLifespan",
                             "rememberMe", "ssoSessionIdleTimeoutRememberMe",
                             "ssoSessionMaxLifespanRememberMe")


def desired_realm_session_tier(db: Session) -> Dict[str, Any]:
    try:
        return {
            "ssoSessionIdleTimeout": int(psr.get_setting(
                USER_TIER_SETTING_KEYS["idle"], db=db) or 0),
            "ssoSessionMaxLifespan": int(psr.get_setting(
                USER_TIER_SETTING_KEYS["max"], db=db) or 0),
            "rememberMe": bool(psr.get_setting(
                "auth.remember_me_enabled", db=db)),
            "ssoSessionIdleTimeoutRememberMe": int(psr.get_setting(
                "auth.remember_me_idle_seconds", db=db) or 0),
            "ssoSessionMaxLifespanRememberMe": int(psr.get_setting(
                "auth.remember_me_max_lifetime_seconds", db=db) or 0),
        }
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"setting read failed: {exc}") from exc


def converge_session_tier(db: Session, kc_client: Any) -> Dict[str, Any]:
    """Converge the realm's session-tier + remember-me fields on the live
    settings. Returns ``{"applied": bool, "desired": {...}, "changed": bool,
    "note": str}``; never raises — a Keycloak failure is reported in ``note``
    and left to keycloak-init.sh to re-converge."""
    result: Dict[str, Any] = {"applied": False, "desired": {},
                              "changed": False, "note": ""}
    try:
        desired = desired_realm_session_tier(db)
        result["desired"] = desired
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"setting read failed: {exc}"
        return result

    try:
        resp = httpx.get(kc_client.admin_api_url,
                         headers={"Authorization":
                                  f"Bearer {kc_client._get_token()}"},
                         timeout=30)
        if resp.status_code != 200:
            result["note"] = f"kc error: realm GET -> HTTP {resp.status_code}"
            return result
        existing = resp.json()
        changed = [f for f in desired if existing.get(f) != desired[f]]
        if not changed:
            result.update(applied=True, note="already converged")
            return result

        body: Dict[str, Any] = {
            "realm": existing.get("realm")
                     or kc_client.admin_api_url.rstrip("/").rsplit("/", 1)[-1],
        }
        for field in psr._REALM_PUT_CARRY_THROUGH_FIELDS:
            if field in existing:
                body[field] = existing[field]
        body.update(desired)
        put = httpx.put(kc_client.admin_api_url,
                        headers={"Authorization":
                                 f"Bearer {kc_client._get_token()}"},
                        json=body, timeout=30)
        if put.status_code in (200, 204):
            result.update(applied=True, changed=True,
                          note=f"converged fields: {sorted(changed)}")
        else:
            result["note"] = f"kc error: realm PUT -> HTTP {put.status_code}"
        return result
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"{type(exc).__name__}: {exc}"
        return result


def tier_for_response(db: Session, user: models.User) -> Dict[str, Any]:
    """The effective session tier for the current caller, for the /me response
    (the Dashboard/WB idle-warning dialog reads it). Never raises — a settings
    failure reports the registered defaults so the dialog still has numbers."""
    tier_key = "admin" if getattr(user, "is_platform_admin", False) else "user"
    try:
        if tier_key == "admin":
            idle = int(psr.get_setting(ADMIN_IDLE_SETTING_KEY, db=db) or 0)
            max_life = int(psr.get_setting(ADMIN_MAX_SETTING_KEY, db=db) or 0)
        else:
            idle = int(psr.get_setting(USER_TIER_SETTING_KEYS["idle"], db=db) or 0)
            max_life = int(psr.get_setting(USER_TIER_SETTING_KEYS["max"], db=db) or 0)
        return {"tier": tier_key,
                "idle_seconds": idle,
                "max_lifetime_seconds": max_life,
                "warning_seconds_before": 120}
    except Exception as exc:  # noqa: BLE001 — a read failure must not 500 /me
        logger.warning("FX.54: session_tier read failed for /me (defaults): %s", exc)
        return {"tier": tier_key, "idle_seconds": None,
                "max_lifetime_seconds": None, "warning_seconds_before": 120}
