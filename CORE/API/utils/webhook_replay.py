"""Webhook replay / dedup helper (R-091 Postmark bounces, R-092 GitHub
webhooks).

Both inbound webhooks are authenticated (HMAC / provider signature) but
carry no nonce or timestamp, so an attacker who captures one valid request
can replay it at will. This helper provides a 24 h single-use claim on a
caller-computed dedup key:

    first time a key is seen  -> True  (apply the delivery)
    already seen (replay)     -> False (ack 200, no side effects)

Redis-down behaviour is deliberately fail-OPEN: a missing or broken cache
returns True (process normally) and logs a warning — bounces and webhook
deliveries must not be silently dropped because the dedup store is down.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: 24 h replay window (task spec for both R-091 and R-092).
REPLAY_TTL_SEC = 24 * 60 * 60

_client = None


def _get_redis():
    """Lazy Valkey/Redis connection (same conventions as
    utils/rate_limiter._get_redis). Returns None when unavailable."""
    global _client
    if _client is None:
        try:
            import redis
            _client = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://redis:6379/2"),
                decode_responses=True,
                socket_connect_timeout=2,
            )
            _client.ping()
        except Exception as e:  # noqa: BLE001
            logger.warning("webhook replay store unavailable: %s", e)
            _client = None
    return _client


def claim_once(key: str, ttl: int = REPLAY_TTL_SEC) -> bool:
    """Claim *key* for *ttl* seconds.

    True  — first time this key was seen: apply the delivery.
    False — key already claimed within the window: replay, ack without
            side effects.
    Redis-down — True (fail-open) with a warning; never drops a delivery.
    """
    r = _get_redis()
    if r is None:
        logger.warning(
            "replay store unavailable — processing delivery without dedup (key %s…)",
            key[:24],
        )
        return True
    try:
        return bool(r.set(key, "1", nx=True, ex=ttl))
    except Exception as e:  # noqa: BLE001
        logger.warning("replay store error — processing without dedup: %s", e)
        return True
