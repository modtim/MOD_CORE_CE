"""Stream tickets — WS5.34b.

Both `GET /v1/notifications/stream` and `GET /v1/wizard-queue/{run_id}/logs/stream`
are SSE endpoints consumed by a browser `EventSource`, which cannot set an
`Authorization` header. Until now both accepted the full Keycloak JWT via
`?access_token=`, which lands in proxy access logs.

This module mints/redeems a short-lived, single-use, opaque ticket instead:
the client calls `POST /v1/stream-tickets` with normal Bearer auth, gets back
a random token, and passes THAT (never the JWT) as `?ticket=` on the
EventSource URL. The ticket is meaningless outside this process — it is a
lookup key into Valkey, not a credential itself.

Storage: Valkey (via the same `redis-py` client / `REDIS_URL` every other
service in this codebase uses — see routers/notifications.py `_redis_url()`).
The raw ticket is NEVER used as the Valkey key; only its sha256 digest is, so
a Valkey dump/backup does not itself hand out live tickets.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from typing import Optional

import redis as redis_lib

logger = logging.getLogger(__name__)

TICKET_TTL_SECONDS = 60
_KEY_PREFIX = "stream-ticket:"

VALID_SCOPES = {"notifications", "wizard-run", "workflow-debug"}


def _redis_url() -> str:
    # Same broker every other Redis/Valkey user in this codebase reuses
    # (see routers/notifications.py:_redis_url).
    return os.getenv("REDIS_URL", "redis://redis:6379/2")


def _redis_client() -> "redis_lib.Redis":
    return redis_lib.Redis.from_url(_redis_url())


def _ticket_key(ticket: str) -> str:
    digest = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


def legacy_token_query_enabled() -> bool:
    """Feature flag gating the deprecated `?access_token=<JWT>` fallback on
    both SSE endpoints. Defaults True for one release so already-cached
    frontend bundles keep working mid-deploy; flip to false once clients
    have migrated to `?ticket=`."""
    from services import platform_settings as _psr
    return bool(_psr.get_setting("sse_legacy_token_query_enabled"))


def mint_ticket(
    *,
    user_id: int,
    tenant_id: int,
    scope: str,
    run_id: Optional[str] = None,
    ttl_seconds: int = TICKET_TTL_SECONDS,
) -> str:
    """Create and store a single-use ticket, returning the RAW value.

    The raw ticket is handed to the caller exactly once here and must never
    be logged or persisted anywhere else — only its sha256 digest is stored,
    as the Valkey key.
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"invalid stream-ticket scope: {scope!r}")
    ticket = secrets.token_urlsafe(32)
    payload = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "scope": scope,
        "run_id": run_id,
    }
    client = _redis_client()
    client.set(_ticket_key(ticket), json.dumps(payload), ex=ttl_seconds)
    return ticket


def redeem_ticket(ticket: str) -> Optional[dict]:
    """Atomically fetch-and-delete a ticket (single-use). Returns the stored
    payload dict, or None if the ticket is missing, expired, or already
    redeemed. Never raises on a bad/absent ticket — callers treat None as
    "invalid ticket" with no further detail.
    """
    if not ticket:
        return None
    key = _ticket_key(ticket)
    client = _redis_client()
    try:
        raw = client.getdel(key)  # atomic GET+DEL, Redis/Valkey >= 6.2
    except (AttributeError, redis_lib.exceptions.ResponseError):
        # Older client/server without GETDEL: pipeline get+delete. Still a
        # single round trip; the ticket is consumed either way even if two
        # racing redeemers both see the value (both then run scope/access
        # checks against the same still-valid payload, so this only matters
        # for a true simultaneous double-spend, which the 60s TTL + one-shot
        # nature of these tickets makes a non-issue in practice).
        pipe = client.pipeline()
        pipe.get(key)
        pipe.delete(key)
        raw, _ = pipe.execute()
    if raw is None:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.warning("stream ticket payload failed to decode: %s", e)
        return None
