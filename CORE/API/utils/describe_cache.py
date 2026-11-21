"""δ Phase 3 — Redis-backed cache for describe-search results.

Why caching matters here: GitHub's authenticated rate limit is
5000 req/h shared across the deployment. Even with a token, 100
tenants describing nodes a few times each can blow through that.
Caching the search-result envelope keyed on the raw description
turns repeat queries (which are common — users iterate on the
same idea) into zero-cost hits.

Key shape: ``mod:describe:search:v1:{sha256}`` where the digest
is over ``description.lower().strip()`` so trivial variations
collapse to the same cache entry. TTL is 24 hours — long enough
to absorb burst traffic, short enough that a repo's
licence/archived state can't go stale beyond a day.

Cache values are JSON-encoded structures owned by the caller
(Phase 6). This module is intentionally dumb about shape — store
arbitrary JSON-serialisable dict, get the same dict back.

Storage isolation: cache is GLOBAL (not per-tenant) on purpose.
The same description should always produce the same candidates
regardless of who asked, and a global cache amortises GitHub
calls across the whole deployment.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Bump the version segment whenever the discovery/ranking logic changes in a way
# that would make a cached response stale — the old namespace is simply orphaned
# and expires on its own TTL. v2 (2026-08-30): curated-repo licence backfill
# (COLMAP/Meshroom no longer dropped) + crossover authority floor. v3
# (2026-09-03): relevance-dominant composite (0.9/0.1, was 0.7/0.3 — fame no
# longer beats fit) + web/PHP ecosystem demotion in describe_score. v4
# (2026-09-03): crossover no longer false-matches the diffusion entry for plain
# "image" queries (a grayscale search was flooding huggingface/diffusers).
CACHE_KEY_PREFIX = "mod:describe:search:v4:"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h


def _client_factory():
    """Lazy import + connect so the module is importable in tests
    that monkeypatch the redis client without a live broker."""
    import redis  # local import — pytest can pre-mock the module
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)


def _digest(description: str) -> str:
    norm = (description or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def cache_key(description: str) -> str:
    return f"{CACHE_KEY_PREFIX}{_digest(description)}"


def lookup(
    description: str,
    *,
    client_factory=_client_factory,
) -> Optional[Dict[str, Any]]:
    """Return the cached payload for ``description`` or None on
    miss / Redis error. Errors are swallowed so a temporary Redis
    blip can't break describe-search — we degrade to "always miss"
    until Redis is back."""
    if not description or not description.strip():
        return None
    try:
        client = client_factory()
        raw = client.get(cache_key(description))
    except Exception as e:
        logger.warning("describe-cache lookup failed (treating as miss): %s", e)
        return None
    if raw is None:
        return None
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except Exception as e:
        logger.warning("describe-cache returned unparseable JSON: %s", e)
        return None


def store(
    description: str,
    payload: Dict[str, Any],
    *,
    ttl: int = CACHE_TTL_SECONDS,
    client_factory=_client_factory,
) -> bool:
    """Persist ``payload`` for ``description`` with the given TTL.
    Returns True on success, False on any error. Errors are
    swallowed because caching is best-effort."""
    if not description or not description.strip():
        return False
    try:
        client = client_factory()
        client.set(
            cache_key(description),
            json.dumps(payload, separators=(",", ":")),
            ex=ttl,
        )
        return True
    except Exception as e:
        logger.warning("describe-cache store failed: %s", e)
        return False


def invalidate(
    description: str,
    *,
    client_factory=_client_factory,
) -> bool:
    """Delete the cache entry for ``description``. Used by Phase
    11 when a repo's metadata may have changed (e.g. an admin
    flagged a previously-cached candidate as blocked)."""
    if not description or not description.strip():
        return False
    try:
        client = client_factory()
        client.delete(cache_key(description))
        return True
    except Exception as e:
        logger.warning("describe-cache invalidate failed: %s", e)
        return False
