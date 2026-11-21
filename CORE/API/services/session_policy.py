"""FX.54 p3a (owner Q37=A + Q49=A) — pure session-tier policy evaluator.

FX.54 p1 put the tier numbers in the platform-settings registry
(``auth.sso_session_*`` = the human-USER tier, ``auth.admin_sso_session_*`` = the
platform-admin tier, ``auth.remember_me_*`` = the 7-day remember-me pair) and
p2 (services/session_tier_enforcement.py) wired the live 401 + audit + activity
stamps on the auth path. Neither part had a *decision* function: the enforcement
path mixes the verdict with side effects (redis stamps, audit rows, HTTP 401),
so nothing downstream — the Dashboard/WB idle-warning dialog (FX.54.p4), the
live remember-me policy-change cut (FX.54.p3c), the Keycloak realm push (FX.54.p3d)
— can ask "is this session inside its tier limits, and which limits apply?"
without re-deciding the tier arithmetic by hand in three places.

``evaluate_session_policy`` is that decision, as a PURE function: session facts
in, verdict + the applicable limits out. No DB writes, no audit rows, no redis,
no exceptions, no HTTP. p3b keeps calling the effectful p2 gate for the request
path; this module is the single source of truth for the numbers every other
consumer reads.

Tier resolution (most permissive last):
  * ``platform_admin`` -> auth.admin_sso_session_* (30 min / 8 h)
  * ``tenant_admin``   -> auth.tenant_admin_sso_session_* — registered HERE
    (platform_settings.py is off-limits to this lane); the defaults match the
    platform-admin pair, i.e. tenant admins get the admin-tier limits today,
    and the separate keys are what make the two tiers tunable apart later.
  * ``normal``         -> auth.sso_session_* (60 min / 12 h)
Remember-me (owner Q49=A "<= 7 days") extends ONLY the normal tier: an admin
session that ticked "stay signed in" is still bounded by the admin pair, which
is exactly what the p2 gate enforces and what the tests here pin.

A limit of 0/None means "off" (same convention as p2). A settings read that
raises fails OPEN for that limit and is reported in ``settings_errors`` so the
caller can log it — never silently treated as "expired".
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

import services.platform_settings as _psr_reg

logger = logging.getLogger(__name__)

PLATFORM_ADMIN_TIER = "platform_admin"
TENANT_ADMIN_TIER = "tenant_admin"
NORMAL_TIER = "normal"
ALLOW = "allow"
REJECT_IDLE = "reject_idle"
REJECT_ABSOLUTE = "reject_absolute"

# The tier -> setting-key map. The admin + user pairs are FX.54 p1's (already
# registered in services/platform_settings.py); only the tenant-admin pair is
# new, and it is registered in-module below (the registry-side file is off-
# limits to this lane; register() is a no-op on re-registration).
def _validate_non_negative_seconds(value: Any) -> None:
    """0 = limit off; negative = nonsense (same shape as the p1 admin pair).

    Defined here rather than reusing platform_settings._validate_non_negative_int:
    that private helper is redefined twice in the registry file (the second copy
    carries an api-key error message), so borrowing it would mis-report the key.
    """
    if int(value) < 0:
        raise ValueError(f"{value!r} must be a non-negative number of seconds")


TIER_SETTING_KEYS: Dict[str, Dict[str, str]] = {
    PLATFORM_ADMIN_TIER: {
        "idle": "auth.admin_sso_session_idle_seconds",
        "absolute": "auth.admin_sso_session_max_lifetime_seconds",
    },
    TENANT_ADMIN_TIER: {
        "idle": "auth.tenant_admin_sso_session_idle_seconds",
        "absolute": "auth.tenant_admin_sso_session_max_lifetime_seconds",
    },
    NORMAL_TIER: {
        "idle": "auth.sso_session_idle_seconds",
        "absolute": "auth.sso_session_max_lifetime_seconds",
    },
}
REMEMBER_ME_SETTING_KEYS = {
    "enabled": "auth.remember_me_enabled",
    "idle": "auth.remember_me_idle_seconds",
    "absolute": "auth.remember_me_max_lifetime_seconds",
}

_psr_reg.register(
    TIER_SETTING_KEYS[TENANT_ADMIN_TIER]["idle"],
    int,
    1800,
    validator=_validate_non_negative_seconds,
    group="auth",
    description="FX.54 p3a — tenant-admin-tier SSO idle timeout in seconds "
                "(30 min). Tenant owners/admins (minio_tenant_admin) get the "
                "ADMIN-tier limits today, so this default matches "
                "auth.admin_sso_session_idle_seconds; the separate key exists "
                "so the two admin tiers can be tuned apart without touching "
                "the platform-admin pair. Read by "
                "services/session_policy.tier_limits.",
)
_psr_reg.register(
    TIER_SETTING_KEYS[TENANT_ADMIN_TIER]["absolute"],
    int,
    28800,
    validator=_validate_non_negative_seconds,
    group="auth",
    description="FX.54 p3a — tenant-admin-tier SSO max lifetime in seconds "
                "(8 h, owner Q49=A). Same rationale as "
                "auth.tenant_admin_sso_session_idle_seconds: admin-tier limits "
                "by default, tunable per tier.",
)


def _to_epoch(value: Any) -> int | None:
    """Epoch seconds for an int/float, ISO string, or datetime. None = absent."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    elif isinstance(value, datetime):
        dt = value
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def tier_for(user_tier: Any) -> str:
    """Normalise the caller's tier fact to one of the three tier names."""
    if isinstance(user_tier, str):
        key = user_tier.strip().lower()
        if key in TIER_SETTING_KEYS:
            return key
        if key in {"admin", "platform", "platform-admin", "super_admin"}:
            return PLATFORM_ADMIN_TIER
        if key in {"tenant", "tenant-admin", "owner", "tenant_owner"}:
            return TENANT_ADMIN_TIER
        return NORMAL_TIER
    if isinstance(user_tier, bool):
        return PLATFORM_ADMIN_TIER if user_tier else NORMAL_TIER
    return NORMAL_TIER


def tier_limits(tier: str, db=None) -> Dict[str, Any]:
    """The (idle, absolute) pair a tier is held to, plus read failures.

    Pure read: one settings lookup per key, no writes. 0/None = limit off.
    """
    keys = TIER_SETTING_KEYS[tier_for(tier)]
    out: Dict[str, Any] = {"tier": tier_for(tier), "errors": []}
    for name, key in (("idle", keys["idle"]), ("absolute", keys["absolute"])):
        try:
            raw = _psr_reg.get_setting(key, db=db)
            out[name] = int(raw) if raw is not None else None
        except Exception as exc:  # noqa: BLE001 — fail open, report it
            logger.warning("FX.54 p3a: setting read failed for %s (fail-open): %s",
                           key, exc)
            out[name] = None
            out["errors"].append(key)
    return out


def evaluate_session_policy(user_tier: Any, issued_at: Any,
                            last_activity_at: Any = None,
                            remember_me: bool = False, now: Any = None,
                            db=None) -> Dict[str, Any]:
    """Decide one session against its tier limits — PURE, never raises.

    ``user_tier``: 'platform_admin' / 'tenant_admin' / 'normal' (also accepts
    the p2-style boolean ``is_platform_admin``). ``issued_at``: the session
    issue/login time (``auth_time`` claim). ``last_activity_at``: the last
    sighting of the session (p2's activity stamp); None = first sighting, so
    the idle window starts now and idle can never reject. ``remember_me``: the
    user's "stay signed in" opt-in — it extends the NORMAL tier only.

    Returns ``{"decision": ALLOW|REJECT_IDLE|REJECT_ABSOLUTE, "tier": ...,
    "idle_seconds"/"absolute_seconds": the tier pair, "effective_idle_seconds"/
    "effective_absolute_seconds": what actually applied after remember-me,
    "age_seconds"/"idle_elapsed_seconds": the measured ages,
    "remember_me_applied": bool, "settings_errors": [...]}``.
    """
    tier = tier_for(user_tier)
    limits = tier_limits(tier, db=db)
    idle, absolute = limits["idle"], limits["absolute"]

    applied_idle, applied_absolute = idle, absolute
    applied_remember = False
    if tier == NORMAL_TIER and remember_me:
        try:
            rm_enabled = bool(_psr_reg.get_setting(
                REMEMBER_ME_SETTING_KEYS["enabled"], db=db))
        except Exception as exc:  # noqa: BLE001 — fail open, report it
            logger.warning("FX.54 p3a: remember-me toggle read failed: %s", exc)
            limits["errors"].append(REMEMBER_ME_SETTING_KEYS["enabled"])
            rm_enabled = False
        if rm_enabled:
            try:
                rm_idle = int(_psr_reg.get_setting(
                    REMEMBER_ME_SETTING_KEYS["idle"], db=db) or 0)
                rm_abs = int(_psr_reg.get_setting(
                    REMEMBER_ME_SETTING_KEYS["absolute"], db=db) or 0)
                applied_idle = max(idle or 0, rm_idle) or None
                applied_absolute = max(absolute or 0, rm_abs) or None
                applied_remember = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("FX.54 p3a: remember-me pair read failed: %s", exc)
                limits["errors"].append(REMEMBER_ME_SETTING_KEYS["idle"])
                limits["errors"].append(REMEMBER_ME_SETTING_KEYS["absolute"])

    epoch_now = _to_epoch(now) if now is not None else int(time.time())
    issued = _to_epoch(issued_at)
    last = _to_epoch(last_activity_at) if last_activity_at is not None else issued

    age = (epoch_now - issued) if (issued is not None and epoch_now is not None) else None
    idle_elapsed = (epoch_now - last) if (last is not None and epoch_now is not None) else None

    decision, over = ALLOW, 0
    if applied_absolute and applied_absolute > 0 and age is not None and age > applied_absolute:
        decision, over = REJECT_ABSOLUTE, age
    elif applied_idle and applied_idle > 0 and idle_elapsed is not None and idle_elapsed > applied_idle:
        decision, over = REJECT_IDLE, idle_elapsed

    return {
        "decision": decision,
        "tier": tier,
        "idle_seconds": idle,
        "absolute_seconds": absolute,
        "effective_idle_seconds": applied_idle,
        "effective_absolute_seconds": applied_absolute,
        "age_seconds": age,
        "idle_elapsed_seconds": idle_elapsed,
        "seconds_over": over,
        "remember_me_applied": applied_remember,
        "settings_errors": limits["errors"],
    }
