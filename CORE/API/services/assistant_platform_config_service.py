"""Service layer for GE.MAA.2 (mod-assistant-agent §9.6/9.7) — the
platform-admin surface for the assistant's PLATFORM-WIDE defaults.

This is deliberately NOT the per-tenant `AssistantTenantConfig` table (owned
by ``services/assistant_router_service.py`` / ``routers/assistant.py``'s
tenant-scoped ``/assistant/config``). It is the layer ABOVE that: a single
set of platform defaults a tenant's config falls back to / is capped by.

Storage: reuses the EXISTING per-tool Vault KV store `routers/ai_config.py`
already maintains for the "assistant" tool (`platform/ai-config/assistant`,
via ``_read_vault_path``/``_write_vault_path``/``_vault_path_for``). That
store already carries the tool's `model` (used by
`services/assistant_orchestrator.py:_resolve_env` as the platform-default
`LLM_MODEL` whenever a tenant hasn't set `AssistantTenantConfig.model_id`)
and `execution_mode`. Rather than open a second, competing store for the
same "assistant" domain (which would need reconciling with that one), this
module adds a few new keys to the SAME dict:

  - `admin_enabled`     (bool)      platform-wide kill switch, ANDed into
                                     every tenant's effective `enabled` by
                                     `assistant_router_service.get_config()`.
  - `model`              (str|None) platform-default model id — the same
                                     key `_resolved_effective_env` already
                                     reads; writing it here IS setting the
                                     platform default, no separate field.
  - `chat_rate_limit_max`       (int|None) override for
    `chat_rate_limit_window_s` (int|None)  ASSISTANT_CHAT_RATE_LIMIT_MAX /
                                            _WINDOW_S (env is the default;
                                            a non-null override here wins —
                                            read by
                                            `utils/rate_limiter.py:check_assistant_chat_rate_limit`).

No new table, no migration — this is exactly the "EXISTING settings
mechanism" grep turned up.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

# Re-export so callers only need this module.
from services.assistant_router_service import ValidationError  # noqa: F401

_RATE_MAX_MIN, _RATE_MAX_MAX = 1, 10000
_RATE_WINDOW_MIN, _RATE_WINDOW_MAX = 10, 86400


def _vault_path() -> str:
    from routers import ai_config
    return ai_config._vault_path_for("assistant")


# Read on every assistant chat (rate limiter + kill-switch), so cache the
# Vault read briefly; PUT invalidates it.
_READ_CACHE_TTL_S = 5.0
_read_cache: Dict[str, Any] = {"ts": 0.0, "raw": None}
_read_cache_lock = threading.Lock()


def _read_raw() -> dict:
    from routers import ai_config
    now = time.monotonic()
    with _read_cache_lock:
        if _read_cache["raw"] is not None and now - _read_cache["ts"] < _READ_CACHE_TTL_S:
            return dict(_read_cache["raw"])
    try:
        raw = dict(ai_config._read_vault_path(_vault_path()) or {})
    except Exception:
        # Vault unreachable — behave like an unconfigured store (platform
        # defaults apply). Mirrors _read_tool_config's own fail-open shape.
        return {}
    with _read_cache_lock:
        _read_cache.update(ts=now, raw=raw)
    return dict(raw)


def _write_raw(raw: dict) -> None:
    from routers import ai_config
    ai_config._write_vault_path(_vault_path(), raw)
    with _read_cache_lock:
        _read_cache.update(ts=0.0, raw=None)


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "").strip() or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def get_config() -> Dict[str, Any]:
    """Return the platform-wide assistant defaults + the currently
    effective (override-or-env) rate-limit numbers, so the admin panel can
    show both "what's pinned" and "what actually applies right now"."""
    raw = _read_raw()
    override_max = raw.get("chat_rate_limit_max")
    override_window = raw.get("chat_rate_limit_window_s")
    return {
        "enabled": bool(raw.get("admin_enabled", True)),
        "model_id": raw.get("model") or None,
        "chat_rate_limit_max": override_max,
        "chat_rate_limit_window_s": override_window,
        "effective_chat_rate_limit_max": int(
            override_max if override_max is not None
            else _env_int("ASSISTANT_CHAT_RATE_LIMIT_MAX", 30)
        ),
        "effective_chat_rate_limit_window_s": int(
            override_window if override_window is not None
            else _env_int("ASSISTANT_CHAT_RATE_LIMIT_WINDOW_S", 60)
        ),
    }


def is_platform_enabled() -> bool:
    """Cheap read used by `assistant_router_service.get_config()` to AND the
    platform kill-switch into every tenant's effective `enabled`. Fails open
    (True) — a Vault hiccup must not silently disable the assistant for
    every tenant."""
    try:
        return bool(_read_raw().get("admin_enabled", True))
    except Exception:
        return True


def update_config(db: Session, *, actor_id: int, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a partial update to the platform-wide config, audit the
    deltas via the existing `audit.log_admin_config_change` helper, and
    return the new effective shape.

    `updates` is the router's already-validated ``_PlatformConfigUpdate``
    dump (`exclude_unset=True`) — numeric range validation happens at the
    Pydantic layer (422), this layer only computes/records deltas.
    """
    raw = _read_raw()
    raw_before = dict(raw)
    deltas: Dict[str, Any] = {}

    def _apply(field: str, new_val: Any, *, raw_key: Optional[str] = None, default: Any = None) -> None:
        key = raw_key or field
        old_val = raw.get(key, default)
        if key == "model" and old_val == "":
            old_val = None
        if old_val != new_val:
            deltas[field] = {"from": old_val, "to": new_val}
        if new_val is None:
            raw.pop(key, None)
        else:
            raw[key] = new_val

    if "enabled" in updates:
        # Matches get_config()'s own default (`raw.get("admin_enabled", True)`)
        # so a first-ever PUT of enabled=True correctly reports a no-op delta
        # instead of a spurious "from: None -> True".
        _apply("enabled", bool(updates["enabled"]), raw_key="admin_enabled", default=True)
    if "model_id" in updates:
        model_id = updates["model_id"] or None
        _apply("model_id", model_id, raw_key="model")
    if "chat_rate_limit_max" in updates:
        _apply("chat_rate_limit_max", updates["chat_rate_limit_max"])
    if "chat_rate_limit_window_s" in updates:
        _apply("chat_rate_limit_window_s", updates["chat_rate_limit_window_s"])

    _write_raw(raw)

    from audit import log_admin_config_change
    log_admin_config_change(
        db,
        admin_user_id=actor_id,
        config_key="assistant.platform_config",
        old_value=raw_before,
        new_value=raw,
        metadata={"deltas": deltas} if deltas else {"deltas": {}, "no_op": True},
    )

    return get_config()
