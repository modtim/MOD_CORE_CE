"""
Audit Middleware — logs every authenticated API request to audit_events.

Compliance: ISO 27001 A.12.4.1, CMMC AU-2, SOC2 CC6.1, TPN CS-7.
Every API call by an authenticated user is recorded with:
- Who (user_id, tenant_id)
- What (method, path, status code)
- When (timestamp)
- Where (IP address, user agent)
- Context (request_id for correlation)

Non-blocking: writes happen in a background thread to avoid latency.
Skip paths: health checks, static files, unauthenticated endpoints.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Paths to skip (high-frequency, no user context, or pre-auth)
SKIP_PREFIXES = (
    "/docs", "/openapi.json", "/redoc",
    "/health", "/metrics", "/platform/features",
    "/_next/", "/static/", "/favicon",
)

SKIP_EXACT = {"/", "/docs", "/redoc"}


def _client_ip(request: Request) -> str:
    """GSEC R-011 / FX.106 p3a — real client IP via the trusted-proxy resolver.
    CE ships utils/client_ip.py (trusted_client_ip); the Paid tree adds
    services/client_ip.py (resolve_client_ip, CF-Connecting-IP support), which
    doesn't exist in a CE build, so it's imported via utils/paid_import.optional_paid
    with a fallback to the CE resolver. XFF / X-Real-IP / CF-Connecting-IP are
    honoured only from a peer inside a trusted-proxy range (net.trusted_proxy_cidrs
    UNION the FX.106 p3a ranges setting network.trusted_proxy_cidrs)."""
    from utils.client_ip import trusted_client_ip as _ce_resolve_client_ip
    from utils.paid_import import optional_paid

    _paid_client_ip = optional_paid("services.client_ip")
    resolve_client_ip = (
        _paid_client_ip.resolve_client_ip if _paid_client_ip is not None
        else _ce_resolve_client_ip
    )
    return resolve_client_ip(request) or "unknown"

# Sensitive paths that should be logged but with extra redaction
SENSITIVE_PATHS = ("/secrets/", "/auth/", "/api-keys/")

# FIX-AUDIT-MIDDLEWARE-REDACT-QUERY-VALUES-OF-SENSITIVE-KEYS: query keys whose
# VALUE must never land in audit_events.details.query. utils.secret_redaction
# SENSITIVE_FIELDS is the platform vocabulary; the extra names below are the
# query-param shapes the routers actually accept (e.g. worker enrollment takes
# ?bootstrap_token=, which is a live enrolment secret).
try:
    from utils.secret_redaction import SENSITIVE_FIELDS as _SR_SENSITIVE_FIELDS
except Exception:  # CE/paid import guard: never let a redaction helper break auditing
    _SR_SENSITIVE_FIELDS = []

QUERY_REDACT_KEYS = frozenset(
    {str(k).lower() for k in _SR_SENSITIVE_FIELDS}
    | {"token", "access_token", "bootstrap_token", "secret", "password",
       "key", "code", "ticket"}
)


def _redact_query(query: str) -> str:
    """FIX-AUDIT-MIDDLEWARE-REDACT-QUERY-VALUES-OF-SENSITIVE-KEYS.

    Replace the VALUE with "[REDACTED]" for every key=value pair whose key
    (case-insensitive) is in QUERY_REDACT_KEYS; leave every other pair
    untouched. Malformed/unparseable query strings fall back to the original
    (truncated) string — a redaction helper must never raise inside the
    request path.
    """
    if not query:
        return query
    try:
        pairs = parse_qsl(query, keep_blank_values=True)
    except Exception:
        return query
    if not pairs:
        return query
    redacted = [
        (k, "[REDACTED]" if str(k).lower() in QUERY_REDACT_KEYS else v)
        for k, v in pairs
    ]
    if all(v != "[REDACTED]" for _, v in redacted):
        # Nothing sensitive: keep the original string byte-for-byte (also
        # covers malformed/unparseable shapes, which we never re-encode).
        return query
    return urlencode(redacted, safe="[]")

# Max queue depth before dropping (prevent memory leak under load)
MAX_QUEUE_DEPTH = 10000

# Batch size for DB writes
BATCH_SIZE = 50
FLUSH_INTERVAL = 5  # seconds


class AuditMiddleware(BaseHTTPMiddleware):
    """Logs every authenticated API request to the audit_events table."""

    def __init__(self, app, db_factory=None):
        super().__init__(app)
        self._db_factory = db_factory
        self._queue = []
        self._lock = threading.Lock()
        self._running = True

        # Start background flush thread
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="audit-flush"
        )
        self._flush_thread.start()

    async def dispatch(self, request: Request, call_next):
        # Skip non-auditable paths. Gate 1: all routers now serve under /v1,
        # so normalize a leading /v1 before matching the (unprefixed) skip lists.
        path = request.url.path
        norm_path = path[3:] if path.startswith("/v1/") else path
        if (
            path in SKIP_EXACT
            or norm_path in SKIP_EXACT
            or any(path.startswith(p) or norm_path.startswith(p) for p in SKIP_PREFIXES)
        ):
            return await call_next(request)

        start_time = time.time()
        response = await call_next(request)
        duration_ms = int((time.time() - start_time) * 1000)

        # Only audit authenticated requests (have user context)
        # Check for auth header presence as a quick filter
        has_auth = (
            request.headers.get("authorization") or
            request.headers.get("x-api-key")
        )
        if not has_auth:
            return response

        # GSEC R-055: attribute the authenticated user. request.state.user_id /
        # .tenant_id are the primary source (set by auth dependencies); where
        # they are not assigned, fall back to decoding the bearer token's
        # `sub` claim directly — the token is already validated by
        # keycloak_auth.get_current_user_claims (JWKS-cached, no extra
        # Keycloak round trip) and the Keycloak id IS the audit actor's
        # identity. Any failure leaves the attribution NULL; the entry still
        # records the client IP + route so the trail never loses the event.
        user_id = getattr(request.state, "user_id", None)
        tenant_id = getattr(request.state, "tenant_id", None)
        kc_user_id = getattr(request.state, "keycloak_user_id", None)
        if user_id is None and kc_user_id is None and tenant_id is None:
            kc_user_id, tenant_id, _attr_err = self._decode_actor(request)
        if tenant_id is None:
            tenant_id = getattr(request.state, "_rate_limit_tenant_id", None)

        # Build audit entry
        entry = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "keycloak_user_id": kc_user_id,
            "event_name": f"{request.method}:{path}",
            "timestamp": datetime.now(timezone.utc),
            "details": {
                "method": request.method,
                "path": path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "ip": _client_ip(request),
                "user_agent": request.headers.get("user-agent", "")[:200],
                "request_id": request.headers.get("x-request-id", ""),
            }
        }
        # GSEC R-055: persist the actor's Keycloak identity so the trail is
        # never a bare NULL user (the integer users.id is resolved from this
        # in _flush where a DB connection is available).
        if kc_user_id is not None:
            entry["details"]["keycloak_user_id"] = kc_user_id

        # Gate 33: attribute an API-key request to the SPECIFIC key, not "session".
        # key_prefix is unique per key. Prefer the prefix stashed on request.state by
        # require_active_and_onboarded_user (covers keys carried as OAuth tokens,
        # azp=api-key-*); fall back to the X-API-Key header prefix.
        api_key_prefix = getattr(request.state, "api_key_prefix", None) \
            or (request.headers.get("x-api-key") or "")[:8] or None
        if api_key_prefix:
            entry["details"]["api_key_prefix"] = api_key_prefix
            entry["details"]["actor_channel"] = "api_key"
        else:
            entry["details"]["actor_channel"] = "session"

        # Redact query params for sensitive paths
        if any(s in path for s in SENSITIVE_PATHS):
            entry["details"]["note"] = "sensitive_path"
        elif request.url.query:
            # FIX-AUDIT-MIDDLEWARE-REDACT-QUERY-VALUES-OF-SENSITIVE-KEYS: the
            # raw query could carry a live secret (e.g. ?bootstrap_token= on the
            # worker enrollment route), so values of sensitive keys are masked
            # before the row is queued. The [:500] cap stays on the raw string.
            entry["details"]["query"] = _redact_query(request.url.query[:500])

        # Add to queue (non-blocking)
        with self._lock:
            if len(self._queue) < MAX_QUEUE_DEPTH:
                self._queue.append(entry)

        return response

    def _decode_actor(self, request):
        """GSEC R-055 — (keycloak_user_id, tenant_id) from the bearer token.

        Decodes the RS256 `sub` claim via keycloak_auth (JWKS is cached — no
        extra Keycloak round trip per request). The token is already
        validated by the request's own auth dependency; a failure here only
        means "unknown actor" (NULL attribution), never a request failure.
        """
        try:
            auth = request.headers.get("authorization") or ""
            if not auth.startswith("Bearer "):
                return None, None, None
            token = auth[7:].strip()
            # Cheap shape guard: a Keycloak JWT is `header.payload.signature`.
            # Anything else (bogus token, an API-key string, …) can't decode,
            # so skip the JWKS round trip entirely and keep the attribution
            # NULL instead of paying a network call for a known-bad token.
            if token.count(".") != 2:
                return None, None, None
            from keycloak_auth import get_current_user_claims
            claims = get_current_user_claims(token)
            return claims.get("sub"), claims.get("tenant_id"), None
        except Exception as e:
            return None, None, e

    def _flush_loop(self):
        """Background thread that flushes audit entries to DB in batches."""
        while self._running:
            time.sleep(FLUSH_INTERVAL)
            self._flush()

    def _flush(self):
        """Write queued entries to database."""
        if not self._db_factory:
            return

        with self._lock:
            if not self._queue:
                return
            batch = self._queue[:BATCH_SIZE]
            self._queue = self._queue[BATCH_SIZE:]

        try:
            from models import AuditEvent
            db = self._db_factory()
            try:
                # GSEC R-055 — resolve the actor's Keycloak id to users.id.
                # Batched (one IN query per flush, distinct ids only); a
                # failure degrades to NULL user_id, never to a lost entry.
                # `User` is imported only when a batch actually carries a
                # keycloak id, so the common all-integer / no-attribution
                # path stays a single `models.AuditEvent` import.
                kc_ids = {e.get("keycloak_user_id") for e in batch
                          if e.get("keycloak_user_id")}
                kc_to_user = {}
                if kc_ids:
                    try:
                        from models import User
                        rows = db.query(User.id, User.keycloak_user_id).filter(
                            User.keycloak_user_id.in_(kc_ids)
                        ).all()
                        kc_to_user = {k: u for u, k in rows}
                    except Exception as e:
                        logger.warning("Audit flush user-lookup failed: %s", e)
                for entry in batch:
                    user_id = entry["user_id"]
                    if user_id is None:
                        user_id = kc_to_user.get(entry.get("keycloak_user_id"))
                    db.add(AuditEvent(
                        tenant_id=entry["tenant_id"],
                        user_id=user_id,
                        event_name=entry["event_name"],
                        timestamp=entry["timestamp"],
                        details=entry["details"],
                    ))
                db.commit()
            except Exception as e:
                logger.warning("Audit flush failed: %s", e)
                db.rollback()
            finally:
                db.close()
        except Exception as e:
            logger.warning("Audit flush DB error: %s", e)
