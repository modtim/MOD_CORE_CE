"""Server-held PKCE verifier store for the OAuth device grant BFF.

Keycloak 26.3 rejects a public client's device authorization request unless it
carries a PKCE code_challenge. The API therefore generates the code_verifier
itself, sends only the S256 challenge to the identity provider, and keeps the
verifier here, keyed by the returned device_code, until the first successful
token exchange or the device code's expiry. The verifier is never returned to
the client and never logged.

Storage: Redis (shared by every uvicorn worker, same REDIS_URL conventions as
utils/rate_limiter). When Redis is unreachable a small in-process TTL dict
(lock-guarded) is used instead; that fallback is per-process, so with several
workers a poll can land on a worker that does not hold the verifier.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_KEY_PREFIX = "device_pkce:"
_MIN_TTL_SEC = 30
_MAX_TTL_SEC = 3600

_redis = None
_last_warn = 0.0
_WARN_INTERVAL_SEC = 60.0
_local: dict = {}
_local_lock = threading.Lock()


def new_verifier() -> str:
    """RFC 7636 code_verifier: 43 unreserved characters from 32 random bytes."""
    return secrets.token_urlsafe(32)


def challenge_for(verifier: str) -> str:
    """S256 code_challenge: base64url(SHA-256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_pair() -> Tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    verifier = new_verifier()
    return verifier, challenge_for(verifier)


def _key(device_code: str) -> str:
    # Hash the device code so the raw value is not a Redis key.
    return _KEY_PREFIX + hashlib.sha256(device_code.encode("utf-8")).hexdigest()


def _warn(msg: str, *args) -> None:
    """Rate-limited warning (a Redis outage must not log on every poll)."""
    global _last_warn
    now = time.monotonic()
    if now - _last_warn >= _WARN_INTERVAL_SEC:
        _last_warn = now
        logger.warning(msg, *args)


def _get_redis():
    global _redis
    if _redis is None:
        try:
            import redis
            client = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://redis:6379/2"),
                decode_responses=True,
                socket_connect_timeout=2,
            )
            client.ping()
            _redis = client
        except Exception as e:  # noqa: BLE001
            _warn("device PKCE store: redis unavailable, using in-process store (%s)",
                  type(e).__name__)
            return None
    return _redis


def _clamp_ttl(ttl_sec) -> int:
    try:
        ttl = int(ttl_sec)
    except (TypeError, ValueError):
        ttl = 600
    return max(_MIN_TTL_SEC, min(_MAX_TTL_SEC, ttl))


def _purge_local(now: float) -> None:
    for k in [k for k, (_, exp) in _local.items() if exp <= now]:
        _local.pop(k, None)


def store_verifier(device_code: str, verifier: str, ttl_sec) -> None:
    """Keep *verifier* for *ttl_sec* seconds (the device code's expires_in)."""
    ttl = _clamp_ttl(ttl_sec)
    r = _get_redis()
    if r is not None:
        try:
            r.set(_key(device_code), verifier, ex=ttl)
            return
        except Exception as e:  # noqa: BLE001
            _warn("device PKCE store: redis write failed, using in-process store (%s)",
                  type(e).__name__)
    with _local_lock:
        now = time.monotonic()
        _purge_local(now)
        _local[_key(device_code)] = (verifier, now + ttl)


def get_verifier(device_code: str) -> Optional[str]:
    """Return the stored verifier (kept until consumed or expired), or None."""
    k = _key(device_code)
    r = _get_redis()
    if r is not None:
        try:
            val = r.get(k)
            if val:
                return val
        except Exception as e:  # noqa: BLE001
            _warn("device PKCE store: redis read failed (%s)", type(e).__name__)
    with _local_lock:
        now = time.monotonic()
        _purge_local(now)
        entry = _local.get(k)
        return entry[0] if entry else None


def consume(device_code: str) -> None:
    """Delete the verifier (after the first successful token or a terminal error)."""
    k = _key(device_code)
    r = _get_redis()
    if r is not None:
        try:
            r.delete(k)
        except Exception as e:  # noqa: BLE001
            _warn("device PKCE store: redis delete failed (%s)", type(e).__name__)
    with _local_lock:
        _local.pop(k, None)
