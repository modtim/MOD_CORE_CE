"""2400GX-n6 (launch-completion-megaplan 30.4.5) — generic through-MOD stack call.

    GET|POST|PUT|PATCH|DELETE /v1/stacks/{stack_id}/services/{service}/call/{path:path}

Owner (10-26): end users call stacks THROUGH MOD, not over it. Any HTTP service
of a stack the caller's tenant owns is reachable with a user token or app key;
the response is streamed back. The basic proxy is CE; metering/quotas/queueing
are a later Paid layer.

Reused seams (no second resolver/signer):
  * auth                  dependencies.require_bearer_or_api_key
  * tenant pin + read     services.persistent_stacks_shared_service.get_owned_stack
                          (the exact stack fetch every persistent_stacks route
                          uses: cross-tenant id -> 404, never 403)
  * upstream resolution   services.app_routing.resolve_upstream (stack-service)
  * caller identity       services.http_triggers.mint_call_token (GX.S7)

App keys are NOT allow-listed here (30.4.6 follows 2400GX-n2): they hit the
default-deny until that rule lands.
"""
from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

import models
from dependencies import get_db, require_bearer_or_api_key

from product_names import themed_logger

logger = themed_logger(__name__, 'stacks')

router = APIRouter(prefix="/stacks", tags=["Stack Call"])

MAX_BODY_BYTES = 64 * 1024 * 1024
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 300.0
MAX_STREAM_S = 3600.0

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
})
_REQ_ALLOW = frozenset({"content-type", "accept", "accept-language", "user-agent"})
# Client-supplied headers that a stack could mistake for platform-asserted
# identity are never forwarded (item 12 header-spoofing fix, 11-25): the stack
# must trust only the MOD-signed Authorization token. Any OTHER custom x-*
# header (for example an app's own X-Scarecrow-User-Token) still passes
# byte-identical.
_REQ_X_DENY_PREFIX = (
    "x-mod-", "x-forwarded-",
    "x-user", "x-tenant", "x-auth", "x-remote-", "x-end-user", "x-identity",
    "x-principal", "x-subject", "x-oidc", "x-jwt",
    "x-ms-client-principal", "x-goog-authenticated", "x-amzn-oidc",
    "x-real-user", "x-email", "x-group", "x-role", "x-acting-as",
    "x-on-behalf-of", "x-impersonate", "x-original-user", "x-admin",
    "x-is-admin", "x-customer", "x-account", "x-org", "x-client-cert",
    "x-ssl-client",
    "x-actor", "x-caller", "x-owner", "x-permission", "x-uid", "x-sub",
    "x-whoami",
)
_REQ_X_DENY = frozenset({"x-api-key", "x-real-ip"})
_RESP_DROP = frozenset({"set-cookie", "set-cookie2", "server", "x-powered-by",
                        "content-encoding"})


def _client_factory() -> httpx.AsyncClient:
    """Seam for tests. No redirect following (a 3xx is passed to the caller)."""
    return httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(
        READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S))


def _normalize_path(path: str, raw: str = "") -> str:
    """400 on anything that could steer the call off the resolved service."""
    from urllib.parse import unquote
    if (path or "").startswith("/") or (raw or "").startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid call path")
    cands = [path or "", raw or "", unquote(raw or ""), unquote(unquote(raw or ""))]
    for c in cands:
        low = c.lower()
        if ("\\" in c or "\x00" in c or "@" in c or "://" in c or "%00" in low
                or "%5c" in low or "%2f" in low or c.startswith("//")):
            raise HTTPException(status_code=400, detail="Invalid call path")
        if any(seg in ("..", ".") for seg in c.split("/")):
            raise HTTPException(status_code=400, detail="Invalid call path")
    return "/" + (path or "").lstrip("/")


def _declared_port(stack: models.PersistentStack, service: str) -> Optional[int]:
    svc = ((stack.compose_spec or {}).get("services") or {}).get(service)
    if not isinstance(svc, dict):
        return None
    for key in ("ports", "expose"):
        for item in svc.get(key) or []:
            raw = item.get("target") if isinstance(item, dict) else item
            raw = str(raw).split("/")[0].split(":")[-1]
            if raw.isdigit() and 1 <= int(raw) <= 65535:
                return int(raw)
    return None


def _token_jti(token: Optional[str]) -> Optional[str]:
    """The ``jti`` of a token MOD just minted (read back, unverified: we are the
    signer). Only the id is returned/recorded, never the token itself."""
    if not token:
        return None
    try:
        seg = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        jti = claims.get("jti")
        return str(jti)[:64] if jti else None
    except Exception:  # noqa: BLE001
        return None


STACK_CALL_TOKEN_TTL_S = 120  # Q-6: one in-flight call; trigger fires keep 300


def _identity_claim_enabled(db: Session) -> bool:
    """Platform switch ``stacks.call_identity_claim_enabled`` (default False).
    A settings read failure degrades to OFF (never breaks the call)."""
    try:
        from services import platform_settings as _ps
        return bool(_ps.get_setting("stacks.call_identity_claim_enabled", db=db))
    except Exception:  # noqa: BLE001
        return False


def _caller_claims(request: Request, db: Session, user: models.User,
                   stack_id: int, service: str) -> Dict[str, Any]:
    """Caller-identity claims for the token (Q-3: user id only, never a name or
    email; Q-7: an API-key call names the key OWNER plus sub_kind=api_key and
    the key id, never any key material)."""
    # The channel and key id come from the auth dependency's RESULT
    # (request.state, written by require_bearer_or_api_key._mark), never from
    # re-reading headers: a user session plus a junk X-API-Key header is a user.
    state = getattr(request, "state", None)
    channel = getattr(state, "auth_channel", None)
    key_id = getattr(state, "auth_api_key_id", None)
    if channel is None:
        # No marker (a dependency override in tests / an auth path that did not
        # mark): contextvar if the platform set one, else "unknown".
        from utils import request_principal as _rp
        channel = _rp.get_channel()
        _k = _rp.get_api_key()
        key_id = getattr(_k, "id", None) if _k is not None else None
    if channel == "stack_identity":
        sub_kind = "service"
    elif channel == "programmatic":
        sub_kind = "api_key"
    elif channel == "interactive":
        sub_kind = "user"
    else:
        sub_kind = "unknown"
    claims: Dict[str, Any] = {
        "sub": f"user:{int(user.id)}", "sub_kind": sub_kind,
        "via": channel if channel in ("interactive", "programmatic",
                                      "stack_identity") else "unknown",
        "stack_id": int(stack_id), "service": service, "scope": "caller.identity",
    }
    if sub_kind == "api_key" and key_id is not None:
        claims["key_id"] = int(key_id)
    return claims


def _audit(db: Session, user: models.User, stack_id: int, service: str,
           method: str, path: str, status: Optional[int],
           bytes_in: int = 0, started: Optional[float] = None,
           caller_jti: Optional[str] = None) -> None:
    try:
        details = {"stack_id": stack_id, "service": service, "method": method,
                   "path": path[:500], "status": status, "bytes_in": bytes_in,
                   "duration_ms": int((time.time() - started) * 1000) if started else None}
        if caller_jti:
            details["caller_jti"] = caller_jti
        db.add(models.AuditEvent(
            tenant_id=user.tenant_id, user_id=user.id, event_name="stack_call",
            details=details,
        ))
        db.commit()
    except Exception:  # noqa: BLE001 — audit must never break the call
        db.rollback()
        logger.exception("stack_call: audit write failed")


def _upstream_base(db: Session, user: models.User, stack_id: int, service: str):
    try:
        from services import app_routing
        from services import persistent_stacks_shared_service as _svc
    except ImportError:  # CE tree without the stack modules
        raise HTTPException(status_code=404, detail="Not found")
    stack = _svc.get_owned_stack(db, stack_id, user)  # tenant pin -> 404
    if service not in ((stack.compose_spec or {}).get("services") or {}):
        raise HTTPException(status_code=404, detail="Stack service not found")
    port = _declared_port(stack, service)
    if not app_routing._stack_running(stack):
        raise HTTPException(status_code=503, detail="Stack is not running")
    route = SimpleNamespace(target_kind="stack-service", stack_service=service,
                            stack_port=port or 0)
    base = app_routing.resolve_upstream(route, stack)
    if not base:
        raise HTTPException(status_code=503,
                            detail="Stack service has no routable endpoint")
    return stack, base


def _relay_for(db: Session, stack, service: str, port: Optional[int],
               method: str, path: str):
    """The mesh-relay hop for a stack hosted on a REMOTE worker.

    STACK-CALL-MESH-RELAY: the API container has no route to the site tailnet
    (100.96.0.0/12) and cannot resolve MagicDNS, so a remote-worker stack's
    published host port is unreachable from here (the 502). The relay node IS
    reachable — it sits on the private internal network `mod_mesh-relay-net`,
    which only the API joins — and dials the target through its own tailnet
    node. Returns (base_url, claim_headers) or None.

    The target/port come ONLY from DB rows (the tenant-pinned stack row + the
    hosting worker's site address), never from user input, and the pair is
    HMAC-signed per call so the relay can only dial what was declared here.
    """
    try:
        from services import mesh_relay
    except ImportError:  # CE tree without the relay module
        return None
    claim = mesh_relay.build_claim(stack, service, port, method, path, db=db)
    if not claim:
        return None
    return (mesh_relay.relay_base_url(), mesh_relay.claim_headers(claim, method, path))


def _forward_headers(request: Request, token: Optional[str]) -> Dict[str, str]:
    out = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in _REQ_ALLOW or (lk.startswith("x-") and lk not in _REQ_X_DENY
                                and not lk.startswith(_REQ_X_DENY_PREFIX)):
            out[k] = v
    if token:
        out["Authorization"] = "Bearer " + token
    return out


def _response_headers(resp: httpx.Response) -> Dict[str, str]:
    out = {}
    for k, v in resp.headers.items():
        lk = k.lower()
        if lk in _HOP_BY_HOP or lk in _RESP_DROP:
            continue
        if lk == "location" and not (v.startswith("/") and not v.startswith("//")):
            continue
        out[k] = v
    return out


@router.api_route(
    "/{stack_id}/services/{service}/call/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    summary="Call an HTTP service of a stack through MOD (streamed)",
)
async def stack_call(
    stack_id: int,
    service: str,
    path: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    started = time.time()
    method = request.method.upper()
    raw = request.scope.get("raw_path", b"") or b""
    raw_s = raw.decode("latin-1").split("/call/", 1)[-1] if b"/call/" in raw else ""
    norm = _normalize_path(path, raw_s)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    parts, size = [], 0
    async for part in request.stream():  # cap while reading (chunked uploads have no length)
        size += len(part)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
        parts.append(part)
    body = b"".join(parts)

    try:
        stack, base = _upstream_base(db, current_user, stack_id, service)
    except HTTPException as exc:
        if exc.status_code == 503:
            _audit(db, current_user, stack_id, service, method, norm, 503,
                   len(body), started)
        raise

    from services.http_triggers import mint_call_token
    claims = (_caller_claims(request, db, current_user, stack.id, service)
              if _identity_claim_enabled(db) else None)
    token = mint_call_token(tenant_id=current_user.tenant_id, target_kind="stack",
                            target_id=stack.id, trigger_id=0, body=body,
                            fired_at=int(started),
                            ttl_seconds=STACK_CALL_TOKEN_TTL_S,
                            extra_claims=claims)
    url = base.rstrip("/") + norm
    extra_headers = {}
    relay = _relay_for(db, stack, service, _declared_port(stack, service),
                       method, norm)
    if relay:
        base, extra_headers = relay
        url = base.rstrip("/") + norm
    client = _client_factory()
    try:
        upstream_req = client.build_request(
            method, url, params=list(request.query_params.multi_items()),
            headers={**_forward_headers(request, token), **extra_headers},
            content=body or None)
        resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("stack_call: upstream unreachable stack=%s svc=%s: %s",
                       stack_id, service, type(exc).__name__)
        _audit(db, current_user, stack_id, service, method, norm, 502,
               len(body), started, caller_jti=_token_jti(token))
        raise HTTPException(status_code=502, detail="Stack service unreachable")

    _audit(db, current_user, stack_id, service, method, norm, resp.status_code,
           len(body), started, caller_jti=_token_jti(token))

    async def _close() -> None:
        await resp.aclose()
        await client.aclose()

    async def _gen():
        # decoded bytes (Content-Encoding is dropped); max-duration cap; close on exit
        deadline = time.monotonic() + MAX_STREAM_S
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
                if time.monotonic() > deadline:
                    break
        finally:
            await _close()

    return StreamingResponse(_gen(), status_code=resp.status_code,
                             headers=_response_headers(resp),
                             background=BackgroundTask(_close))
