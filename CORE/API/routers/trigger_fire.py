"""add-workflow-triggers §5 — public fire endpoints for inbound (push) triggers.

These are UNAUTHENTICATED-by-default routes (no Keycloak) — each carries its own
per-trigger secret/signature instead. They map the inbound request to an
`event_ctx` and run it through the shared evaluation pipeline (conditions, dedup,
rate-limit), which is the single place abuse protection + dispatch live.

§5.1 webhook is implemented here; §5.2–5.5 (api/email/chatops/git) extend this file.

Gate 2b service/CRUD migration: this module is now HTTP-only (parse/validate
input, shape the response). Data access (`db.query`) lives in
`crud/trigger_fire.py`; business/policy logic (shared-token checks, signature
verification, the tenant-scoping 404 trap, dispatch orchestration) lives in
`services/trigger_fire_service.py`. Every handler below keeps its original
signature, docstring, decorator, and tags — only the body changed, to a thin
delegation.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_current_user, get_db
from services import trigger_fire_service
from services._base import ServiceError

router = APIRouter(prefix="/triggers", tags=["Trigger Fire"])


# Response models: every field Optional, extra keys allowed, and the routes use
# response_model_exclude_unset=True, so these document the existing payloads
# without adding, dropping or reordering a key. Handlers that return a
# JSONResponse (webhook/api/manual/approval/form) use response_model=None.

class FiredResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    fired: Optional[int] = None


class GitFireResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    fired: Optional[int] = None
    reason: Optional[str] = None


class StreamingStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    message_bus: Optional[Dict[str, Dict[str, Any]]] = None
    poll_active: Optional[int] = None


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer `ServiceError` to the exact `HTTPException` this
    router raised directly before the Gate 2b migration (same status code +
    plain-string detail — every error this domain raises is a plain string,
    never a structured dict)."""
    status = {"not_found": 404, "unauthorized": 401, "forbidden": 403}.get(exc.code, 400)
    raise HTTPException(status_code=status, detail=exc.message)


def _bearer_or_header_token(request: Request, fallback: Optional[str]) -> Optional[str]:
    """Extract a shared token from the request. MinIO's `notify_webhook` delivers
    its configured `auth_token` in the `Authorization` header (it prepends
    `Bearer ` when the token carries no scheme), so a real bucket notification
    arrives as `Authorization: Bearer <token>`. Direct/internal callers may
    instead present the channel header (e.g. `X-Notify-Token`). Prefer the
    Authorization bearer; fall back to the channel header."""
    auth = request.headers.get("authorization") or ""
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return auth.strip()
    return fallback


_MAX_FIRE_BODY = int(os.getenv("TRIGGER_MAX_BODY_BYTES", str(1 << 20)))  # 1 MiB default


async def _parse_body(request: Request) -> Dict[str, Any]:
    """Best-effort body parse → dict. JSON object as-is; otherwise wrapped. Caps the
    body size (§10.1 abuse guard)."""
    raw = await request.body()
    if len(raw) > _MAX_FIRE_BODY:
        raise HTTPException(status_code=413, detail="trigger payload too large")
    if not raw:
        return {}
    ctype = request.headers.get("content-type", "")
    try:
        if "application/json" in ctype:
            import json
            data = json.loads(raw)
            return data if isinstance(data, dict) else {"value": data}
        if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
            form = await request.form()
            return dict(form)
    except Exception:  # noqa: BLE001 — malformed body is not fatal; pass it as text
        pass
    return {"raw": raw.decode("utf-8", "replace")[:65536]}


# api-lint: public-by-design: per-trigger `{token}` bcrypt-verified against configuration.secret_hash via verify_api_key, services/trigger_fire_service.py:211 (get_valid_webhook_trigger:199-213)
@router.post("/webhook/{trigger_id}/{token}", response_model=None)
async def fire_webhook(trigger_id: int, token: str, request: Request,
                       db: Session = Depends(get_db)):
    """Fire a webhook trigger. The `{token}` is the per-trigger secret issued at
    create time (bcrypt-hashed in `configuration.secret_hash`)."""
    try:
        trig, forward = trigger_fire_service.get_valid_webhook_trigger(db, trigger_id, token)
    except ServiceError as exc:
        _raise_http(exc)
    body = await _parse_body(request) if forward else {}
    status_code, resp = trigger_fire_service.dispatch_webhook(db, trig, body, forward)
    return _json(status_code, resp)


def _json(status: int, content: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=status, content=content)


# --- §8 streaming status (honest degraded reporting) -----------------------

@router.get("/streaming-status", response_model=StreamingStatusResponse,
            response_model_exclude_unset=True)
def streaming_status(db: Session = Depends(get_db),
                     user: models.User = Depends(get_current_user)):
    """Which message-bus providers are actually available (client installed) and how
    many active triggers each has, plus active poll-trigger count. Surfaces DEGRADED
    providers honestly rather than pretending they work.

    1420 R2-C: this reads platform-wide (not tenant-scoped) trigger counts and
    had NO auth at all. The underlying query (services/triggers/streaming.py:
    streaming_status) has no tenant_id filter by design — it reports system-wide
    message-bus configuration health, not any one tenant's data — so tenant
    scoping isn't the right fix; there's also no Dashboard/WorkflowBuilder caller
    today (grepped both, no hits), so this gets the stricter of the two options
    the review raised: platform-admin only, same `is_platform_admin` gate used
    throughout CORE/API/routers/admin_*.py."""
    if not getattr(user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="platform admin required")
    return trigger_fire_service.streaming_status(db)


# --- §6.1 cloud: MinIO bucket-notification webhook -------------------------

# remove-minio-versitygw P1 — storage-neutral canonical route. `/triggers/storage-event`
# is primary; the legacy `/triggers/minio` route below is kept as an alias so webhook
# senders still configured against the old URL keep working, authenticated the SAME way
# (same shared secret, same constant-time check, same handler body).
# api-lint: public-by-design: MINIO_NOTIFY_TOKEN shared secret checked constant-time via check_shared_token/_shared_token_ok (hmac.compare_digest, services/trigger_fire_service.py:93-97 + :81-90), routers/trigger_fire.py:fire_storage_event
@router.post("/storage-event", response_model=FiredResponse, response_model_exclude_unset=True)
async def fire_storage_event(request: Request,
                             x_notify_token: Optional[str] = Header(None),
                             db: Session = Depends(get_db)):
    """Ingest an S3 bucket-notification payload and fire matching file_event
    triggers (source=s3, legacy alias minio). Gated by MINIO_NOTIFY_TOKEN, presented
    either as `Authorization: Bearer <token>` (what the bucket-notification target
    sends) or the `X-Notify-Token` header (direct callers). Fail-closed in SaaS."""
    presented = _bearer_or_header_token(request, x_notify_token)
    try:
        trigger_fire_service.check_shared_token("MINIO_NOTIFY_TOKEN", presented,
                                                message="invalid notify token")
    except ServiceError as exc:
        _raise_http(exc)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected JSON S3 event")
    total = trigger_fire_service.dispatch_s3_records(db, payload)
    return {"fired": total}


# api-lint: public-by-design: MINIO_NOTIFY_TOKEN shared secret checked constant-time via check_shared_token/_shared_token_ok (hmac.compare_digest, services/trigger_fire_service.py:93-97 + :81-90), routers/trigger_fire.py:162-166
@router.post("/minio", response_model=FiredResponse, response_model_exclude_unset=True)
async def fire_minio_event(request: Request,
                           x_notify_token: Optional[str] = Header(None),
                           db: Session = Depends(get_db)):
    """Ingest a MinIO/S3 bucket-notification payload and fire matching file_event
    triggers (source=minio). Gated by MINIO_NOTIFY_TOKEN, presented either as
    `Authorization: Bearer <token>` (what MinIO's webhook target sends) or the
    `X-Notify-Token` header (direct callers). Fail-closed in SaaS."""
    presented = _bearer_or_header_token(request, x_notify_token)
    try:
        trigger_fire_service.check_shared_token("MINIO_NOTIFY_TOKEN", presented,
                                                message="invalid notify token")
    except ServiceError as exc:
        _raise_http(exc)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="expected JSON S3 event")
    total = trigger_fire_service.dispatch_minio_records(db, payload)
    return {"fired": total}


# --- §6.2 on-prem: worker filesystem watcher → API -------------------------

class FsEvent(BaseModel):
    path: str = Field(..., description="watched root the worker reported")
    key: str = Field(..., description="relative file path under the watched root")
    event: str = "created"  # created|modified|deleted


# api-lint: public-by-design: TRIGGER_FS_TOKEN shared secret checked constant-time via check_shared_token/_shared_token_ok (hmac.compare_digest, services/trigger_fire_service.py:93-97, :81-90), routers/trigger_fire.py:188-192
@router.post("/fs-event", response_model=FiredResponse, response_model_exclude_unset=True)
def fire_fs_event(ev: FsEvent,
                  x_worker_token: Optional[str] = Header(None),
                  db: Session = Depends(get_db)):
    """Ingest a filesystem event from the on-prem worker watcher and fire matching
    file_event triggers (source=fs). Gate with TRIGGER_FS_TOKEN (header X-Worker-Token)."""
    try:
        trigger_fire_service.check_shared_token("TRIGGER_FS_TOKEN", x_worker_token,
                                                message="invalid worker token")
    except ServiceError as exc:
        _raise_http(exc)
    fired = trigger_fire_service.dispatch_fs_event(db, ev.path, ev.key, ev.event)
    return {"fired": fired}


# --- §5.2 authenticated API trigger ---------------------------------------

@router.post("/api/{trigger_id}", response_model=None)
def fire_api(trigger_id: int, payload: Dict[str, Any] = Body(default={}),
             user: models.User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    """Authenticated, tenant-scoped fire — the caller's own workflows only."""
    try:
        status_code, resp = trigger_fire_service.fire_api(db, trigger_id, payload, user)
    except ServiceError as exc:
        _raise_http(exc)
    return _json(status_code, resp)


# --- §5.3 inbound email ----------------------------------------------------

# api-lint: public-by-design: TRIGGER_EMAIL_TOKEN shared secret checked constant-time via check_shared_token/_shared_token_ok (hmac.compare_digest, services/trigger_fire_service.py:93-97 + :81-90), routers/trigger_fire.py:218-221
@router.post("/email", response_model=FiredResponse, response_model_exclude_unset=True)
async def fire_email(request: Request, x_email_token: Optional[str] = Header(None),
                     db: Session = Depends(get_db)):
    """Inbound-email webhook (SES/mailbox). Matches EMAIL triggers by `to` address."""
    try:
        trigger_fire_service.check_shared_token("TRIGGER_EMAIL_TOKEN", x_email_token,
                                                message="invalid email token")
    except ServiceError as exc:
        _raise_http(exc)
    body = await _parse_body(request)
    # 432 (BUG 1) — the pipeline's 7.2 retry backoff sleeps; offload the whole
    # synchronous fire so it never freezes the event loop (same offload pattern
    # as routers/notifications.py). The `db` session from Depends(get_db) is
    # only used inside that one call — the existing pattern elsewhere.
    fired = await asyncio.to_thread(trigger_fire_service.fire_email, db, body)
    return {"fired": fired}


# --- §5.5 git / CI ---------------------------------------------------------

# api-lint: public-by-design: GitHub HMAC-SHA256 signature verified via _verify_github (hmac.compare_digest, services/trigger_fire_service.py:100-104) enforced at fire_git services/trigger_fire_service.py:305-306; GitLab token via hmac.compare_digest at :315
@router.post("/git/{trigger_id}", response_model=GitFireResponse,
             response_model_exclude_unset=True)
async def fire_git(trigger_id: int, request: Request,
                   x_hub_signature_256: Optional[str] = Header(None),
                   x_github_event: Optional[str] = Header(None),
                   x_gitlab_token: Optional[str] = Header(None),
                   x_gitlab_event: Optional[str] = Header(None),
                   x_github_delivery: Optional[str] = Header(None),
                   db: Session = Depends(get_db)):
    """Verified git/CI provider webhook (GitHub HMAC-SHA256 / GitLab token), with
    event + branch filters. R-092: GitHub `X-GitHub-Delivery` ids are
    deduped in redis (24 h) — a replayed delivery is 200-acked without
    re-firing."""
    raw = await request.body()
    try:
        # 432 (BUG 1) — offload the synchronous fire (retry backoff sleeps).
        return await asyncio.to_thread(
            trigger_fire_service.fire_git,
            db, trigger_id, raw, x_hub_signature_256, x_github_event,
            x_gitlab_token, x_gitlab_event, x_github_delivery)
    except ServiceError as exc:
        _raise_http(exc)


# --- §5.4 chat-ops (Slack / Teams) ----------------------------------------

# api-lint: public-by-design: per-trigger signing_secret verified per provider — Slack v0 HMAC via _verify_slack (hmac.compare_digest, services/trigger_fire_service.py:107-117) / Teams HMAC via _verify_teams (:120-128); non-matching triggers skipped, nothing dispatched, fire_chatops services/trigger_fire_service.py:362-365
@router.post("/chatops/{provider}", response_model=FiredResponse,
             response_model_exclude_unset=True)
async def fire_chatops(provider: str, request: Request,
                       x_slack_signature: Optional[str] = Header(None),
                       x_slack_request_timestamp: Optional[str] = Header(None),
                       authorization: Optional[str] = Header(None),
                       db: Session = Depends(get_db)):
    """Verified chat-ops command (Slack v0 HMAC / Teams outgoing-webhook HMAC)."""
    raw = await request.body()
    body = await _parse_body(request)
    try:
        # 432 (BUG 1) — offload the synchronous fire (retry backoff sleeps).
        fired = await asyncio.to_thread(
            trigger_fire_service.fire_chatops,
            db, provider, raw, body, x_slack_signature, x_slack_request_timestamp,
            authorization)
    except ServiceError as exc:
        _raise_http(exc)
    return {"fired": fired}


# --- §7 manual / human -----------------------------------------------------

@router.post("/manual/{trigger_id}", response_model=None)
def fire_manual(trigger_id: int, payload: Dict[str, Any] = Body(default={}),
                user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """Manual fire (the Dashboard 'Run now' button) — authenticated, tenant-scoped."""
    try:
        status_code, resp = trigger_fire_service.fire_manual(db, trigger_id, payload, user)
    except ServiceError as exc:
        _raise_http(exc)
    return _json(status_code, resp)


class ApprovalDecision(BaseModel):
    decision: str = Field(..., description="approve | reject")
    note: Optional[str] = None


@router.post("/approval/{trigger_id}", response_model=None)
def fire_approval(trigger_id: int, body: ApprovalDecision,
                  user: models.User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """Human-in-the-loop: fire only when an authorized approver approves."""
    try:
        status_code, resp = trigger_fire_service.fire_approval(
            db, trigger_id, body.decision, body.note, user)
    except ServiceError as exc:
        _raise_http(exc)
    return _json(status_code, resp)


# api-lint: public-by-design: per-trigger `{token}` bcrypt-verified against configuration.secret_hash via verify_api_key (security/__init__.py:28-35), services/trigger_fire_service.py:419 (get_valid_form_trigger:411-421)
@router.post("/form/{trigger_id}/{token}", response_model=None)
async def fire_form(trigger_id: int, token: str, request: Request,
                    db: Session = Depends(get_db)):
    """Public form submission — per-trigger token (issued at create), form/JSON → inputs."""
    try:
        trig = trigger_fire_service.get_valid_form_trigger(db, trigger_id, token)
    except ServiceError as exc:
        _raise_http(exc)
    body = await _parse_body(request)
    status_code, resp = trigger_fire_service.dispatch_form(db, trig, body)
    return _json(status_code, resp)
