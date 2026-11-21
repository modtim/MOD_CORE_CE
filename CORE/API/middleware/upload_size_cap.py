"""GSEC R-155 — API-side upload size cap.

Pure ASGI middleware (deliberately NOT ``BaseHTTPMiddleware``) so it can
wrap ``receive`` and account for real body bytes — including chunked /
``Transfer-Encoding: chunked`` uploads that never carry a Content-Length.

Behaviour
---------
* Only body-carrying methods (POST/PUT/PATCH) are considered; GET/HEAD/
  OPTIONS (and everything not under ``/v1``) pass through untouched.
* If the declared ``Content-Length`` already exceeds the class cap the
  request is rejected with ``413`` immediately — the body is never read.
* Otherwise the body is counted byte-by-byte as it streams in; once the
  running total crosses the cap the middleware sends ``413`` (if the
  response has not started) and serves ``http.disconnect`` to the app so
  it stops reading.  Late app responses are suppressed.

Caps come from the platform-settings registry (group ``storage``) via
``services.platform_settings.get_setting`` with a ~5 s in-process cache.
Any settings-read error falls back to :data:`DEFAULT_CAPS` (fail safe:
still capped).

Per-tenant overrides (GSEC R-155 follow-up)
-------------------------------------------
The tenant id is derived ONLY from the request's AUTH — never from a
client header/query.  A bearer JWT is verified with the exact same
helper the auth dependency uses (``keycloak_auth.get_current_user_claims``
— RS256 against the TTL-cached JWKS, so this is a cheap in-process
verify with no extra Keycloak round trip; the audit middleware already
uses this pattern).  If verification fails (expired / foreign-signed /
garbage token, realm down) or the claims carry no tenant, resolution is
platform scope — i.e. the pre-follow-up behaviour.

The effective cap is ``min(platform_cap, tenant_cap)``: a tenant may
only LOWER its upload cap, never raise one above the platform ceiling,
so an unverifiable token can never loosen anything.  Tenant caps are
cached for 5 s like the platform caps.

The ``embed`` class is fixed at ``routers.run_surface_public.
MAX_EMBED_UPLOAD_BYTES`` (100 MB); this is what actually enforces that
constant (previously only defined, never referenced by a cap).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Optional, Tuple

# The embed cap is intentionally imported from the public run surface
# module so the two can never drift.  Guarded only for the (hypothetical)
# CE build where that router is absent — falls back to the same 100 MB.
try:
    from routers.run_surface_public import MAX_EMBED_UPLOAD_BYTES
except Exception:  # pragma: no cover - CE build without public run surface
    MAX_EMBED_UPLOAD_BYTES = 100 * 1024 * 1024

from services.platform_settings import get_setting as _get_setting

# Fail-safe defaults (mirror the registry defaults in
# services/platform_settings.py, group "storage").
DEFAULT_CAPS: Dict[str, int] = {
    "uploads.max_bytes_default": 100 * 1024 * 1024,      # 100 MB
    "uploads.max_bytes_files": 512 * 1024 * 1024,        # 512 MB
    "uploads.max_bytes_cog": 1024 * 1024 * 1024,         # 1 GB
    "uploads.max_bytes_node_build": 512 * 1024 * 1024,   # 512 MB
}

_CLASS_SETTING = {
    "default": "uploads.max_bytes_default",
    "files": "uploads.max_bytes_files",
    "cog": "uploads.max_bytes_cog",
    "node_build": "uploads.max_bytes_node_build",
}

# Paths exempt from the cap.  Each is a multi-GB, internally trusted
# model-cache pass-through mirrored by the nginx 20g S3 upload class, so
# capping it in the API would break legitimate admin/worker transfers
# without adding real protection (both are auth-gated upstream).
EXEMPT_PREFIXES: Tuple[str, ...] = (
    "/v1/internal/model-cache/",              # worker<->API model blob sync (internal)
    "/v1/system/model-cache/import-bundle",   # admin-gated multi-GB bundle import
)

_COG_UPLOAD_RE = re.compile(r"^/v1/cogs/.*/(upload-file|uploads)(?:/|$)")
_RUN_LINK_UPLOAD_RE = re.compile(r"^/v1/run-links/[^/]+/upload(?:/)?$")
_WORKFLOW_UPLOAD_MARKERS = ("import-zip", "upload-processed-file", "upload-text-file")


def classify(path: str) -> Optional[str]:
    """Map a request path to its cap class.

    Returns ``None`` (not managed here), ``"exempt"``, or one of
    ``default/files/cog/embed/node_build``.
    """
    if not path.startswith("/v1"):
        return None
    if any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES):
        return "exempt"
    if path in ("/v1/files/upload", "/v1/files/upload/"):
        return "files"
    if _COG_UPLOAD_RE.match(path):
        return "cog"
    if path in ("/v1/runs/ephemeral-inputs", "/v1/runs/ephemeral-inputs/"):
        return "cog"
    if path.startswith("/v1/road/") and any(
        marker in path for marker in _WORKFLOW_UPLOAD_MARKERS
    ):
        return "cog"
    if _RUN_LINK_UPLOAD_RE.match(path):
        return "cog"
    if path == "/v1/embed/run" or path.startswith("/v1/embed/run/"):
        return "embed"
    if path.startswith(
        ("/v1/woodsman", "/v1/node-definitions/builds", "/v1/node-authoring")
    ):
        return "node_build"
    return "default"


_CACHE_TTL = 5.0  # settings cache: a change shows on every worker within 5 s
_FALLBACK_TTL = 5.0  # short-cache errors so a down DB is not hit per request
# (key, tenant_id|None) -> (value, expires_monotonic)
_settings_cache: Dict[Tuple[str, Optional[int]], Tuple[int, float]] = {}
# bearer token -> (tenant_id|None, expires_monotonic): the claims→tenant
# resolution is memoized so a verified client does not pay a JWT verify
# per upload request.  Bounded; cleared wholesale when it grows.
_tenant_memo: Dict[str, Tuple[Optional[int], float]] = {}
_TENANT_MEMO_MAX = 512


def reset_cap_cache() -> None:
    """Clear the in-process settings/tenant caches (used by tests)."""
    _settings_cache.clear()
    _tenant_memo.clear()


def _platform_cap(key: str) -> int:
    """Platform-scope cap for a settings key (fail-safe to DEFAULT_CAPS)."""
    now = time.monotonic()
    hit = _settings_cache.get((key, None))
    if hit is not None and hit[1] > now:
        return hit[0]
    try:
        value = int(_get_setting(key))
        if value <= 0:
            value = DEFAULT_CAPS[key]
    except Exception:
        # Fail safe: still capped, but do not hammer a broken settings
        # path on every upload request.
        value = DEFAULT_CAPS[key]
        _settings_cache[(key, None)] = (value, now + _FALLBACK_TTL)
        return value
    _settings_cache[(key, None)] = (value, now + _CACHE_TTL)
    return value


def _tenant_cap(key: str, tenant_id: int) -> Optional[int]:
    """Tenant-scope override for a settings key, or None when there is no
    readable tenant row (fail safe: the platform cap governs).  Cached for
    _CACHE_TTL like the platform caps."""
    now = time.monotonic()
    hit = _settings_cache.get((key, tenant_id))
    if hit is not None and hit[1] > now:
        return hit[0]
    try:
        raw = _get_setting(key, tenant_id=tenant_id)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    _settings_cache[(key, tenant_id)] = (value, now + _CACHE_TTL)
    return value


def _class_cap(class_name: str, tenant_id: Optional[int] = None) -> int:
    """Effective cap: min(platform cap, tenant cap).

    A tenant may only LOWER a cap — a tenant row above the platform value
    is ignored, so an unverifiable/absent token can never raise anything.
    """
    if class_name == "embed":
        return int(MAX_EMBED_UPLOAD_BYTES)
    key = _CLASS_SETTING[class_name]
    platform_cap = _platform_cap(key)
    if tenant_id is None:
        return platform_cap
    tenant_cap = _tenant_cap(key, tenant_id)
    if tenant_cap is None:
        return platform_cap
    return min(platform_cap, tenant_cap)


def _tenant_from_scope(scope: Dict[str, Any]) -> Optional[int]:
    """Tenant id derived from the request's AUTH only (never a header/query).

    A bearer JWT is verified with the same helper the auth dependency uses
    (``keycloak_auth.get_current_user_claims`` — RS256 against the TTL-cached
    JWKS, so this is an in-process verify, no extra Keycloak round trip; the
    audit middleware already uses this pattern).  Verification failure, a
    claims set without a tenant, or a non-JWT bearer all resolve to ``None``
    = platform scope (the pre-follow-up behaviour).  Results (including
    negatives) are memoized per token so a verified client does not pay a
    JWT verify per upload request.
    """
    token = None
    for key, value in scope.get("headers") or []:
        name = key.decode() if isinstance(key, bytes) else str(key)
        if name.lower() == "authorization":
            raw = value.decode() if isinstance(value, bytes) else str(value)
            if raw.startswith("Bearer "):
                token = raw[7:].strip()
            break
    if not token or token.count(".") != 2:
        # Shape guard (same as the audit middleware): a non-JWT bearer
        # (an API-key string, garbage) can never yield a tenant.
        return None
    now = time.monotonic()
    memo = _tenant_memo.get(token)
    if memo is not None and memo[1] > now:
        return memo[0]
    tenant_id: Optional[int] = None
    try:
        from keycloak_auth import get_current_user_claims
        claims = get_current_user_claims(token)
        raw_tenant = claims.get("tenant_id")
        if raw_tenant is not None:
            tenant_id = int(raw_tenant)
    except Exception:
        # Expired / foreign-signed / garbage token, realm down, or a claims
        # set without a tenant: never trust the token — platform cap.
        tenant_id = None
    if len(_tenant_memo) >= _TENANT_MEMO_MAX:
        _tenant_memo.clear()
    _tenant_memo[token] = (tenant_id, now + (_CACHE_TTL if tenant_id else _FALLBACK_TTL))
    return tenant_id


def _declared_content_length(scope: Dict[str, Any]) -> Optional[int]:
    for key, value in scope.get("headers") or []:
        if key.lower() == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


async def _send_413(send, cap: int) -> None:
    body = json.dumps({"detail": f"Upload exceeds the {cap} byte limit"}).encode()
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class UploadSizeCapMiddleware:
    """Pure-ASGI upload size cap, per path class (see module docstring)."""

    BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method")
        if method not in self.BODY_METHODS:
            await self.app(scope, receive, send)
            return
        class_name = classify(scope.get("path") or "")
        if class_name is None or class_name == "exempt":
            await self.app(scope, receive, send)
            return
        cap = _class_cap(class_name, _tenant_from_scope(scope))

        # Fast path: declared size already over the cap → 413 without
        # ever reading a single body byte.
        declared = _declared_content_length(scope)
        if declared is not None and declared > cap:
            await _send_413(send, cap)
            return

        state = {
            "count": 0,
            "over": False,
            "responded": False,   # True once WE sent the 413 (suppress app sends)
            "response_started": False,  # True once the app's response began
        }

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                state["response_started"] = True
            if state["responded"]:
                return  # our 413 already went out; drop any late app response
            await send(message)

        async def receive_wrapper():
            if state["over"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") != "http.request":
                return message
            state["count"] += len(message.get("body") or b"")
            if state["count"] > cap and not state["response_started"]:
                state["over"] = True
                await _send_413(send, cap)
                state["responded"] = True
                return {"type": "http.disconnect"}
            return message

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception:
            if state["responded"]:
                # We already sent the 413; the app's reaction to the
                # synthetic disconnect (e.g. starlette ClientDisconnect) is
                # expected and must not become a second (500) response.
                return
            raise
