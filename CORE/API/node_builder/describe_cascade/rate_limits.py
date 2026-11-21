"""wizard-rebuild §8b — per-backend rate-limit infrastructure.

Each describe-cascade backend (Docker Hub, HF Spaces, NGC, GitHub,
PyPI, npm) gets its own per-tenant Redis-backed counter. A burst on
one backend (Docker Hub flapping under heavy use) doesn't drag the
others down with it; the user-facing rate-limit notice can name the
specific backend.

Cache hits don't consume window — call `record_cache_hit(backend,
tenant_id)` to decrement after a cache lookup that didn't trigger
a real backend call.

Keys: ``mod:cascade:<backend>:tenant:{id}``. Each window is per
backend per tenant; backends have independent counters + TTLs.

Defaults (per design.md §8 table):

  Docker Hub search     10 / 15 min
  HuggingFace Spaces    10 / 15 min
  NGC catalog            5 / 15 min
  GitHub repo search     5 / 15 min
  PyPI lookup           20 / 15 min
  npm lookup            20 / 15 min

Redis errors fail open — better to serve a few extra requests than
block the wizard on a Redis blip. Same pattern as the existing
describe-search rate limit.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable

logger = logging.getLogger(__name__)


# Backend → (limit_count, window_seconds)
_DEFAULTS: Dict[str, tuple[int, int]] = {
    "docker_hub":  (10, 15 * 60),
    "hf_spaces":   (10, 15 * 60),
    "ngc":         (5,  15 * 60),
    "github":      (5,  15 * 60),
    "pypi":        (20, 15 * 60),
    "npm":         (20, 15 * 60),
}


def known_backends() -> Iterable[str]:
    """Set of valid backend keys. Used by validators + tests."""
    return tuple(_DEFAULTS.keys())


def _key(backend: str, tenant_id: int) -> str:
    return f"mod:cascade:{backend}:tenant:{int(tenant_id)}"


def _redis():
    """Lazy Redis client. Mirrors the existing /describe-search
    pattern so tests can monkeypatch one factory."""
    import redis  # local import — keep module light at startup
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)


def check_and_inc(backend: str, tenant_id: int) -> Dict[str, Any]:
    """Atomically increment the (backend, tenant) counter and return
    a status dict.

    Returns ``{allowed, remaining, reset_seconds, backend}``. When
    ``allowed`` is False the caller MUST NOT call the backend; the
    user-facing message should mention ``backend`` so the user knows
    which one is rate-limited (not just "cascade").

    Failures (unknown backend, Redis down) fail open — a Redis blip
    shouldn't block the wizard.
    """
    if backend not in _DEFAULTS:
        logger.warning("rate_limits.check_and_inc unknown backend %r — fail open", backend)
        return {
            "allowed": True, "remaining": -1, "reset_seconds": -1,
            "backend": backend,
        }
    limit, window = _DEFAULTS[backend]
    try:
        client = _redis()
        k = _key(backend, tenant_id)
        pipe = client.pipeline()
        pipe.incr(k)
        pipe.ttl(k)
        count, ttl = pipe.execute()
        if int(count) == 1 or int(ttl) < 0:
            client.expire(k, window)
            ttl = window
        if int(count) > limit:
            return {
                "allowed": False, "remaining": 0,
                "reset_seconds": int(ttl), "backend": backend,
            }
        return {
            "allowed": True, "remaining": limit - int(count),
            "reset_seconds": int(ttl), "backend": backend,
        }
    except Exception as exc:
        logger.warning(
            "rate_limits.check_and_inc backend=%s tenant=%s failed (%s) "
            "— fail open", backend, tenant_id, exc,
        )
        return {
            "allowed": True, "remaining": -1, "reset_seconds": -1,
            "backend": backend,
        }


def record_cache_hit(backend: str, tenant_id: int) -> None:
    """Roll back the most recent increment when the call was actually
    served from cache. Idempotent — best-effort; Redis errors are
    swallowed."""
    if backend not in _DEFAULTS:
        return
    try:
        client = _redis()
        k = _key(backend, tenant_id)
        client.decr(k)
    except Exception:
        pass


def snapshot(tenant_id: int) -> Dict[str, Dict[str, Any]]:
    """Return ``{backend: {remaining, reset_seconds, limit}}`` for
    every known backend. Used by the cascade endpoint to surface
    per-backend status to the UI alongside its main result.

    Read-only — does NOT increment counters. Backend not yet
    contacted in this window → ``remaining = limit``."""
    out: Dict[str, Dict[str, Any]] = {}
    for backend, (limit, window) in _DEFAULTS.items():
        out[backend] = {"limit": limit, "remaining": limit, "reset_seconds": -1}
    try:
        client = _redis()
        for backend, (limit, _window) in _DEFAULTS.items():
            try:
                count = client.get(_key(backend, tenant_id))
                count = int(count) if count is not None else 0
                ttl = client.ttl(_key(backend, tenant_id))
                out[backend] = {
                    "limit": limit,
                    "remaining": max(0, limit - count),
                    "reset_seconds": int(ttl) if ttl and ttl >= 0 else -1,
                }
            except Exception:
                pass
    except Exception:
        pass
    return out
