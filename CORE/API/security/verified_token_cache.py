"""Short-lived POSITIVE cache for bcrypt token verifications on the worker task plane.

Why: every /workers/tasks/* call authenticates the worker with
`bcrypt.checkpw(X-Bootstrap-Token, enrollment.bootstrap_token_hash)`. At cost 12
that is ~0.24 s of CPU per request, run inline in an `async def` dependency (so
it also stalls the event loop). Measured 11-25 on xeon: POST /started 0.345 s and
POST /credentials 0.37 s of the per-task claim phase were this one call.

Safety contract (what is unchanged):
  * Only a SUCCESSFUL verification is cached; a wrong token always pays the full
    bcrypt cost (brute-force resistance unchanged) and is never stored.
  * The key binds the token to the exact stored hash: rotating/revoking the token
    (new hash) is an immediate miss. Status / expiry / enrollment-row checks live
    in the caller and still run on every request.
  * The token itself is never stored: only HMAC-SHA256 under a per-process random key.
  * Bounded size, TTL from platform setting `worker.token_verify_cache_ttl_s`
    (default 120 s, clamped to 3600 s). The CURRENT ttl is applied to entries that
    are already stored: 0 (or less) clears the cache and is an instant off switch
    (previous behaviour); lowering it drops entries older than the new ttl.
  * The setting is read through a small in-process cache (_TTL_REFRESH_S) and a
    refresh runs in a worker thread, so no request ever opens a DB session on the
    event loop; a changed setting takes effect within _TTL_REFRESH_S seconds.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import threading
import time

from . import verify_api_key

_MAX_ENTRIES = 512
_DEFAULT_TTL_S = 120.0
_MAX_TTL_S = 3600.0
_TTL_REFRESH_S = 5.0
_KEY = os.urandom(32)
_LOCK = threading.Lock()
_CACHE: dict[bytes, float] = {}  # digest -> stored-at (monotonic seconds)
_TTL_STATE: list = [None, 0.0]  # [ttl seconds or None, monotonic time it was read]


def _digest(token: str, stored_hash: str) -> bytes:
    m = hmac.new(_KEY, b"", hashlib.sha256)
    m.update(token.encode("utf-8"))
    m.update(b"\x00")
    m.update(stored_hash.encode("utf-8"))
    return m.digest()


def _ttl_seconds() -> float:
    """Read the setting (opens a DB session: call from a thread, never the event loop)."""
    try:
        from services.platform_settings import get_setting
        v = get_setting("worker.token_verify_cache_ttl_s")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(min(max(v, 0), _MAX_TTL_S))
    except Exception:  # noqa: BLE001 - unreadable setting: use the default
        pass
    return _DEFAULT_TTL_S


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
        _TTL_STATE[0] = None
        _TTL_STATE[1] = 0.0


def _ttl_cached() -> float | None:
    """The last-read ttl if it is younger than _TTL_REFRESH_S, else None (no DB access)."""
    with _LOCK:
        v, at = _TTL_STATE
        if v is not None and (time.monotonic() - at) < _TTL_REFRESH_S:
            return v
    return None


def _apply_ttl(ttl: float) -> None:
    """Remember a freshly read ttl; 0 clears every entry, a lower ttl drops the older ones."""
    now = time.monotonic()
    with _LOCK:
        _TTL_STATE[0] = ttl
        _TTL_STATE[1] = now
        if ttl <= 0:
            _CACHE.clear()
        else:
            for k in [k for k, at in _CACHE.items() if now - at >= ttl]:
                _CACHE.pop(k, None)


async def _current_ttl() -> float:
    v = _ttl_cached()
    if v is not None:
        return v
    v = await asyncio.to_thread(_ttl_seconds)  # DB read off the event loop
    _apply_ttl(v)
    return v


def _hit(d: bytes, ttl: float) -> bool:
    if ttl <= 0:
        clear()
        return False
    now = time.monotonic()
    with _LOCK:
        at = _CACHE.get(d)
        if at is None:
            return False
        if now - at >= ttl:
            _CACHE.pop(d, None)
            return False
        return True


def _store(d: bytes, ttl: float) -> None:
    if ttl <= 0:
        return
    now = time.monotonic()
    with _LOCK:
        if len(_CACHE) >= _MAX_ENTRIES:
            for k in [k for k, at in _CACHE.items() if now - at >= ttl]:
                _CACHE.pop(k, None)
            if len(_CACHE) >= _MAX_ENTRIES:
                _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[d] = now


async def verify_token_async(token: str, stored_hash: str) -> bool:
    """Same truth value as the bcrypt `verify_api_key`, off the event loop, with a positive cache."""
    if not token or not stored_hash:
        return False
    ttl = await _current_ttl()
    d = _digest(token, stored_hash)
    if _hit(d, ttl):
        return True
    ok = await asyncio.to_thread(verify_api_key, token, stored_hash)
    if ok:
        _store(d, ttl)
    return ok
