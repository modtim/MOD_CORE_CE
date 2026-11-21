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
_REQ_X_DENY_PREFIX = ("x-mod-", "x-forwarded-")
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


def _audit(db: Session, user: models.User, stack_id: int, service: str,
           method: str, path: str, status: Optional[int],
           bytes_in: int = 0, started: Optional[float] = None) -> None:
    try:
        db.add(models.AuditEvent(
            tenant_id=user.tenant_id, user_id=user.id, event_name="stack_call",
            details={"stack_id": stack_id, "service": service, "method": method,
                     "path": path[:500], "status": status, "bytes_in": bytes_in,
                     "duration_ms": int((time.time() - started) * 1000) if started else None},
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
    token = mint_call_token(tenant_id=current_user.tenant_id, target_kind="stack",
                            target_id=stack.id, trigger_id=0, body=body,
                            fired_at=int(started))
    url = base.rstrip("/") + norm
    client = _client_factory()
    try:
        upstream_req = client.build_request(
            method, url, params=list(request.query_params.multi_items()),
            headers=_forward_headers(request, token), content=body or None)
        resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("stack_call: upstream unreachable stack=%s svc=%s: %s",
                       stack_id, service, type(exc).__name__)
        _audit(db, current_user, stack_id, service, method, norm, 502,
               len(body), started)
        raise HTTPException(status_code=502, detail="Stack service unreachable")

    _audit(db, current_user, stack_id, service, method, norm, resp.status_code,
           len(body), started)

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
