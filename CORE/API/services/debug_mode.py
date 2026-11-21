"""GS.PSR.9 — DEBUG_MODE moved onto the DB-backed platform-settings registry.

`DEBUG_MODE` historically meant "log full (potentially sensitive) detail for
ad-hoc troubleshooting." It used to be a per-module import-time constant
computed from the `DEBUG_MODE` env var (`.lower() == "true"`). Those readers
now resolve through `services.platform_settings` (DB > env_fallback >
default), so an operator can flip debug logging per deployment without a
rebuild, while the env var remains as a migration bridge.

Security note: debug logging MAY expose sensitive data (full tokens, keys,
links). This module only relocates where the flag comes from — it does NOT
add any new logging and does not change what debug mode does.

FX.96 part 3 (owner decision Q82=A) — gap G-2: the flag used to stay on for
as long as it was armed, so the DEBUG_MODE-gated raw-secret lines stayed open
indefinitely. `debug.auto_off_minutes` (platform-settings registry, default
60) now bounds it: the "enabled-at" time is the live `platform.debug_mode`
row's `updated_at`, and once that row is older than the window the flag
reads OFF. Nothing is written by the read path — an expired row is still
true in the table, it simply stops being honoured (and the row can be
cleared later via DELETE /v1/platform/settings/platform.debug_mode).

The env fallback has no timestamp, so env-driven debug logging cannot be
aged: the timer only applies to a DB row (the normal admin-UI path).

`is_debug_mode()` is the single call-time reader. On ANY registry/DB error it
falls back to the exact parse the code used to do
(`os.getenv("DEBUG_MODE", "false").lower() == "true"`), so a setting-table or
database outage never changes debug behaviour from what the env says.
"""
from __future__ import annotations

import logging
import os
import time as _time
from datetime import datetime, timezone

from services import platform_settings as _psr

DEBUG_MODE_KEY = "platform.debug_mode"
DEBUG_MODE_ENV = "DEBUG_MODE"
DEFAULT_DEBUG_MODE = False

AUTO_OFF_KEY = "debug.auto_off_minutes"
DEFAULT_AUTO_OFF_MINUTES = 60

AUDIT_ACTION = "ADMIN_FEATURE_FLAG_CHANGE"

logger = logging.getLogger(__name__)


_psr.register(
    DEBUG_MODE_KEY,
    bool,
    DEFAULT_DEBUG_MODE,
    env_fallback=DEBUG_MODE_ENV,
    # main.py still configures third-party log levels from this flag ONCE at
    # boot, so changing the value needs a process restart to take full effect.
    restart_required=True,
    group="platform",
    description="Debug-logging toggle. When on, the API logs full (potentially "
                "sensitive) detail — tokens, keys, links, error tracebacks — for "
                "ad-hoc troubleshooting. Leave off in production: it can leak "
                "secrets into logs. Resolves DB (tenant/platform row) > DEBUG_MODE "
                "env > default (False). FX.96 (Q82=A): a DB row that has been on "
                "longer than `debug.auto_off_minutes` reads as off.",
)


def _legacy_parse() -> bool:
    """The pre-registry parse — identical to every call-site's old
    `os.getenv("DEBUG_MODE", "false").lower() == "true"`."""
    return os.getenv(DEBUG_MODE_ENV, "false").lower() == "true"


def _auto_off_minutes() -> int:
    """The auto-off window in minutes (default 60). Never raises."""
    try:
        raw = _psr.get_setting(AUTO_OFF_KEY)
    except Exception:  # noqa: BLE001 — a settings outage keeps the default
        return DEFAULT_AUTO_OFF_MINUTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_AUTO_OFF_MINUTES
    return value if value > 0 else DEFAULT_AUTO_OFF_MINUTES


AGE_CACHE_SECONDS = 15
_age_cache: "tuple | None" = None  # (checked_at, age_minutes_or_None)


def _debug_row_age_minutes(db) -> "float | None":
    """Minutes since the LIVE platform row for DEBUG_MODE_KEY was written.

    None when there is no live row (then the env fallback decides and there
    is nothing to age). `updated_at` is the enabled-at time: `set_setting`
    soft-supersedes the old row and inserts a fresh one, so the live row's
    timestamp is exactly when the flag was last switched."""
    from models import PlatformSetting

    query = db.query(PlatformSetting).filter(
        PlatformSetting.key == DEBUG_MODE_KEY,
        PlatformSetting.scope == "platform",
        PlatformSetting.tenant_id.is_(None),
        PlatformSetting.deleted_at.is_(None),
    )
    row = query.first()
    if row is None or row.updated_at is None:
        return None
    updated = row.updated_at
    if updated.tzinfo is None:  # naive server_default -> assume UTC
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).total_seconds() / 60.0


def _cached_row_age_minutes() -> "float | None":
    """Row age in minutes, cached for AGE_CACHE_SECONDS.

    `is_debug_mode()` is on the hot path (main.py and several routers call it
    per request), so the age lookup is cached for a few seconds. A row flip is
    seen within that window; the auto-off boundary itself is a minute-scale
    deadline, so a few seconds of slack is immaterial."""
    global _age_cache
    now = _time.monotonic()
    if _age_cache is not None and now - _age_cache[0] < AGE_CACHE_SECONDS:
        return _age_cache[1]
    from database import SessionLocal

    db = SessionLocal()
    try:
        age = _debug_row_age_minutes(db)
    finally:
        db.close()
    _age_cache = (now, age)
    return age


def _resolve() -> bool:
    """DB row > (aged) auto-off > env fallback > default. Raises only if the
    settings subsystem itself fails, which `is_debug_mode()` catches."""
    value = bool(_psr.get_setting(DEBUG_MODE_KEY))
    if not value:
        _age_cache_reset()
        return False
    age = _cached_row_age_minutes()
    if age is None:  # env/default-sourced: nothing to age
        return True
    return age < _auto_off_minutes()


def _age_cache_reset() -> None:
    global _age_cache
    _age_cache = None


def is_debug_mode() -> bool:
    """Call-time debug flag. DB row (aged by `debug.auto_off_minutes`) >
    DEBUG_MODE env > default (False).

    Falls back to the legacy env parse on any registry/DB error, so a
    database or settings-table outage cannot change behaviour from what the
    environment declares."""
    try:
        return _resolve()
    except Exception:  # noqa: BLE001 — must degrade, never raise
        return _legacy_parse()


def set_debug_mode(value: bool, *, db, actor_user_id: int,
                   request=None) -> bool:
    """Enable/disable debug logging through the registry and write the audit
    row for it (G-2: "enabling/disabling writes an audit row").

    The audit action is the existing `AuditAction.ADMIN_FEATURE_FLAG_CHANGE`
    — the same member the platform-settings PATCH router emits for a
    settings write — so both paths produce one comparable row. Secret values
    are never in the metadata: only the key, the boolean and the actor.

    The caller owns the transaction (this flushes, it does not commit).
    Returns the coerced value."""
    coerced = _psr.set_setting(DEBUG_MODE_KEY, value, db=db,
                               actor_user_id=actor_user_id)
    _age_cache_reset()  # the row just moved; the next read must see it fresh
    try:
        import audit
        from models import AuditAction
        audit.log_admin_action(
            db,
            AuditAction.ADMIN_FEATURE_FLAG_CHANGE,
            actor_user_id,
            request=request,
            resource_type="platform_setting",
            metadata={"action": "set", "key": DEBUG_MODE_KEY,
                      "new_value": coerced},
        )
    except Exception:  # noqa: BLE001 — audit emit must not mask the write
        logger.warning("debug-mode audit emit failed (non-fatal)",
                       exc_info=True)
    return coerced
