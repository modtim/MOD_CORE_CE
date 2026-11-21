"""MOD Assistant — HTTP surface.

For now (mod-assistant-agent §2.4) this owns only the platform-admin
hot-reload endpoint for the site map. §5 will land the streaming
POST /assistant/chat endpoint and the session lifecycle endpoints
alongside this router.

Keeping everything assistant-related under a single router file +
prefix makes the eventual rate-limit bucket (`assistant_chat`)
trivial to scope and means the audit-log surfaces don't have to
hunt for the right module.
"""
from __future__ import annotations

import logging
from typing import Dict, Literal, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
# GY.C16 remaining routes: the chat SSE stream echoes tenant data content
# (same gate as routers/ai_assistant.py's /assistant endpoints).
from dependencies import require_auditor_data_content
from utils import assistant_site_map
from utils.http_errors import internal_http_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["Assistant"])


# Response models. Mirrors the notifications.py house style (WS1.R16): every
# field is Optional (default None) and extra keys are allowed, so these
# document the existing service payloads without being able to reject or strip
# one — the shapes below come straight from services/assistant_router_service.py.
# /chat is a StreamingResponse (SSE) so it declares response_model=None.


class AssistantConfigResponse(BaseModel):
    """Shape of AssistantRouterService.get_config / update_config. Every
    field may be present on every path, but Optional keeps the model from
    rejecting the defensive fallback dict returned when the backfilled row
    is missing."""
    model_config = ConfigDict(extra="allow")
    enabled: Optional[bool] = None
    model_id: Optional[str] = None
    system_prompt_override: Optional[str] = None
    action_allowlist: Optional[dict] = None
    logs_context_enabled: Optional[bool] = None
    session_retention_hours: Optional[int] = None
    knowledge_base_enabled: Optional[bool] = None


class AssistantHistoryResponse(BaseModel):
    """Shape of AssistantRouterService.get_history: session_id (None on a
    fresh start) and its scrubbed transcript. `messages` entries are dicts
    of arbitrary shape so the model just has to allow extra keys."""
    model_config = ConfigDict(extra="allow")
    session_id: Optional[str] = None
    messages: Optional[list] = None


class AssistantUsageResponse(BaseModel):
    """Shape of AssistantRouterService.get_usage: aggregate counts plus the
    by_model roll-up (each an opaque dict) and the deferred top_intents."""
    model_config = ConfigDict(extra="allow")
    days: Optional[int] = None
    total_sessions: Optional[int] = None
    total_messages: Optional[int] = None
    by_model: Optional[list] = None
    top_intents: Optional[list] = None


class ReloadSiteMapResponse(BaseModel):
    """Shape of reload_site_map's success dict."""
    model_config = ConfigDict(extra="allow")
    ok: Optional[bool] = None
    page_count: Optional[int] = None
    dialog_count: Optional[int] = None


def _require_platform_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform admin only",
        )
    return current_user


# ----------------------------------------------------------------------------
# GE.MAA.2 (mod-assistant-agent §9.6/9.7) — platform-wide assistant defaults.
# A separate APIRouter (not `router` above) because these live under
# `/platform/assistant`, not `/assistant` — mounted alongside `router` in
# main.py so both share this module. Reuses `_require_platform_admin`
# (defined immediately above, the same dependency `/assistant/site-map/reload`
# already uses) rather than inventing a second platform-admin gate.
# ----------------------------------------------------------------------------

platform_router = APIRouter(prefix="/platform/assistant", tags=["Assistant"])


class PlatformAssistantConfigResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: Optional[bool] = None
    model_id: Optional[str] = None
    chat_rate_limit_max: Optional[int] = None
    chat_rate_limit_window_s: Optional[int] = None
    effective_chat_rate_limit_max: Optional[int] = None
    effective_chat_rate_limit_window_s: Optional[int] = None


class _PlatformConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    model_id: Optional[str] = Field(None, max_length=128)
    # None (omitted) = don't touch; explicit null in JSON also clears an
    # override back to "use the env default" (Optional + exclude_unset lets
    # both through — see the router handler's normalization).
    chat_rate_limit_max: Optional[int] = Field(None, ge=1, le=10000)
    chat_rate_limit_window_s: Optional[int] = Field(None, ge=10, le=86400)


@platform_router.get("/config", response_model=PlatformAssistantConfigResponse)
def get_platform_assistant_config(
    _admin: models.User = Depends(_require_platform_admin),
) -> dict:
    """Return the platform-wide assistant defaults: the kill switch every
    tenant's `enabled` is ANDed against, the platform-default model, and the
    `/assistant/chat` rate-limit knobs (env is the default; a non-null value
    here overrides it — see `utils/rate_limiter.py:check_assistant_chat_rate_limit`).
    """
    from services import assistant_platform_config_service as svc

    return svc.get_config()


@platform_router.put("/config", response_model=PlatformAssistantConfigResponse)
def put_platform_assistant_config(
    body: _PlatformConfigUpdate = Body(...),
    admin: models.User = Depends(_require_platform_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Update the platform-wide assistant defaults. Platform-admin only.
    Every save writes an audit row via `audit.log_admin_config_change`
    (the same helper other platform-admin config surfaces use).

    Fields omitted from the request body are left untouched. An explicit
    empty string for `model_id` clears the override back to NULL.
    """
    from services import assistant_platform_config_service as svc

    updates = body.model_dump(exclude_unset=True)
    if "model_id" in updates and updates["model_id"] == "":
        updates["model_id"] = None

    try:
        return svc.update_config(db, actor_id=admin.id, updates=updates)
    except svc.ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.message,
        )


# mod-assistant-agent §7.6 — the launcher button needs to know
# whether the assistant feature is on for the requesting user's
# tenant before it renders. Returning the full per-tenant config
# (not just `enabled`) here lets the launcher avoid a second
# round-trip when the panel eventually wires in §8 — same
# response shape regardless of caller surface.
#
# The response shape (_row_to_dict), the default-fallback dict, and
# the §9 action-allowlist vocabulary now live in
# services/assistant_router_service.py — only the retention-hours
# bounds stay here, since _ConfigUpdate's Field(...) validators need
# them at class-definition time.
_RETENTION_HOURS_MIN = 1
_RETENTION_HOURS_MAX = 168


class _ConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    model_id: Optional[str] = Field(None, max_length=128)
    system_prompt_override: Optional[str] = Field(None, max_length=8000)
    action_allowlist: Optional[dict] = None
    logs_context_enabled: Optional[bool] = None
    session_retention_hours: Optional[int] = Field(
        None, ge=_RETENTION_HOURS_MIN, le=_RETENTION_HOURS_MAX,
    )
    knowledge_base_enabled: Optional[bool] = None


def _is_tenant_admin(user: models.User) -> bool:
    """Tenant-root-admin or platform-admin can mutate the per-tenant
    config. The codebase identifies the tenant root admin as the
    user with `parent_user_id IS NULL` for their tenant (see
    routers/tenant.py:288 for the same predicate on the data-export
    endpoint). Platform admins always pass.
    """
    if getattr(user, "is_platform_admin", False):
        return True
    return getattr(user, "parent_user_id", None) is None


@router.get("/config", response_model=AssistantConfigResponse)
def get_assistant_config(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return the requesting tenant's assistant config.

    Used by the launcher button to decide whether to render at all
    (`enabled`), and later by §8's panel to know which model the
    chat backend is talking to. The row should always exist because
    database._migrate_schema() backfills one per tenant at startup;
    defensive fallback returns the platform defaults if the row is
    ever missing.
    """
    from services import assistant_router_service as svc

    return svc.get_config(db, current_user.tenant_id)


@router.get("/history", response_model=AssistantHistoryResponse)
def get_assistant_history(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return the requesting user's most-recent ACTIVE assistant session
    (within the tenant's retention window) and its transcript, so a panel
    mounting on ANY surface — Dashboard or WorkflowBuilder, which live on
    different subdomains and can't share sessionStorage — resumes the same
    conversation instead of starting fresh. The session is server-owned per
    (user, tenant), so continuity carries across navigation + surfaces with
    no cross-domain cookie.

    Returns {session_id: None, messages: []} when there's no recent session
    (fresh start). Only user/assistant turns are returned (tool messages are
    internal). `actions` carries the assistant message's action buttons so
    they re-render on resume.
    """
    from services import assistant_router_service as svc

    return svc.get_history(db, user_id=current_user.id, tenant_id=current_user.tenant_id)


@router.put("/config", response_model=AssistantConfigResponse)
def put_assistant_config(
    body: _ConfigUpdate = Body(...),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
) -> dict:
    """Update the requesting tenant's assistant config. Tenant-root-
    admin or platform-admin only. Every save writes an AuditEvent
    capturing the field deltas + the actor.

    Fields omitted from the request body are not updated. To clear
    a string override (e.g. system_prompt_override), pass the empty
    string — it persists as NULL.
    """
    if not _is_tenant_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant admin only",
        )

    from services import assistant_router_service as svc

    try:
        return svc.update_config(
            db,
            tenant_id=current_user.tenant_id,
            actor_id=current_user.id,
            updates=body.model_dump(exclude_unset=True),
        )
    except svc.ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.message,
        )


@router.get("/usage", response_model=AssistantUsageResponse)
def get_assistant_usage(
    days: int = 7,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return aggregate usage for the tenant over the last `days`
    days. Tenant-admin gated — non-admin tenant users can chat but
    can't see the spend roll-up.

    v1 surface: total_sessions, total_messages, by-model token
    spend. The spec's "top intents" item is deferred until §5's
    orchestrator stamps an intent classification on each turn —
    until then there's nothing to roll up.
    """
    if not _is_tenant_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant admin only",
        )

    from services import assistant_router_service as svc

    try:
        return svc.get_usage(db, tenant_id=current_user.tenant_id, days=days)
    except svc.ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.message,
        )


@router.post("/site-map/reload", response_model=ReloadSiteMapResponse)
def reload_site_map(
    _admin: models.User = Depends(_require_platform_admin),
) -> dict:
    """Re-read the site-map YAML from disk and re-validate. On
    validation failure the in-process cache is NOT swapped — the
    prior valid map keeps serving the orchestrator. Returns the new
    counts on success so the caller can confirm the change took.
    """
    try:
        site_map = assistant_site_map.reload_from_disk()
    except assistant_site_map.SiteMapError as e:
        # Log the parse diagnostic server-side only; don't leak the
        # raw file-content error to the client, even though this
        # endpoint is platform-admin only.
        logger.warning("Site-map reload rejected: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Site map file is invalid; see server logs",
        ) from e
    logger.info(
        "Site-map reloaded by admin user_id=%s — %d pages, %d dialogs",
        _admin.id, len(site_map.pages), len(site_map.dialog_id_to_pages),
    )
    return {
        "ok": True,
        "page_count": len(site_map.pages),
        "dialog_count": len(site_map.dialog_id_to_pages),
    }


# Bounds on the confirm_tokens field. The gate's own tokens are
# 64-hex sha256-HMACs, so 512 chars is generous headroom; 10 tools per turn
# matches the orchestrator's per-turn tool-call ceiling.
_CONFIRM_TOKEN_MAX_ITEMS = 10
_CONFIRM_TOKEN_MAX_LEN = 512


def _confirm_token_map(pairs: Optional[List[str]]) -> Optional[Dict[str, str]]:
    """Fold the bounded 'tool=token' list back into the {tool: token} map the
    orchestrator + gate already speak. A pair with no '=' yields an empty
    token, which the gate refuses; nothing here mints or validates tokens."""
    if not pairs:
        return None
    out: Dict[str, str] = {}
    for p in pairs:
        name, sep, tok = p.partition("=")
        if sep and name:
            out[name] = tok
    return out or None


# ----------------------------------------------------------------------------
# §5 real chat endpoint — replaces the §8 stub. The orchestrator
# handles per-tenant gating, the LLM tool-use loop, action validation
# against the site map, session + message persistence, and cost
# telemetry. Endpoint just unpacks the body + wires the streaming
# response.
#
# Falls back to a canned-response pool inside the orchestrator when
# the tenant has no LLM configured, so the panel UI still works
# pre-launch.
# ----------------------------------------------------------------------------


class _ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = Field(None, max_length=64)
    current_page: Optional[str] = Field(None, max_length=255)
    # Which app the user is currently on. The same URL ("/") can
    # belong to either Dashboard or WorkflowBuilder; without this
    # context the site-map lookup is ambiguous and the cross-page
    # wrap can falsely skip itself.
    current_app: Optional[str] = Field(None, max_length=32)
    current_workflow_id: Optional[int] = None
    # The UI's confirm dialog re-POSTs the SAME turn with the
    # one-time tokens the gate handed back (CORE/Dashboard/js/assistant-panel.js
    # and the WorkflowBuilder twin send `confirm_tokens: {tool_name: token}`).
    # The field is a bounded list of str (max 10 tools per turn, 512 chars
    # each — the gate's own tokens are 64-hex sha256-HMACs); the panels'
    # object form is normalized into "tool=token" pairs by the validator
    # below so both spellings are accepted. The orchestrator wants {tool:
    # token}; the router folds it back at the call site. Token VALIDATION is
    # entirely the existing gate's (assistant_confirm.check_tool_call: HMAC
    # bound to user + tool + args hash, single-use store, 5-min TTL) —
    # nothing new is minted or checked here.
    confirm_tokens: Optional[List[str]] = Field(
        None, max_length=_CONFIRM_TOKEN_MAX_ITEMS,
        description="one-time confirm tokens as 'tool_name=token' pairs "
                    "re-sent by the assistant confirm dialog",
    )

    @field_validator("confirm_tokens", mode="before")
    @classmethod
    def _normalize_confirm_tokens(cls, raw):
        """Accept the panels' {tool: token} object and the bounded list of
        'tool=token' strings; anything else is a 422. Length bounds live
        here (item count is pydantic's max_length above); the token itself
        is never parsed or validated — the gate owns that."""
        if raw is None:
            return None
        if isinstance(raw, dict):
            items = [f"{str(k)}={str(v)}" for k, v in raw.items()]
        elif isinstance(raw, list):
            items = [str(x) for x in raw]
        else:
            raise ValueError("confirm_tokens must be a list or object")
        if len(items) > _CONFIRM_TOKEN_MAX_ITEMS:
            raise ValueError(f"too many confirm_tokens (max {_CONFIRM_TOKEN_MAX_ITEMS})")
        if any(len(i) > _CONFIRM_TOKEN_MAX_LEN for i in items):
            raise ValueError(f"confirm_token too long (max {_CONFIRM_TOKEN_MAX_LEN})")
        return items


@router.post("/chat", response_model=None,
             dependencies=[Depends(require_auditor_data_content)])
async def chat(
    body: _ChatRequest = Body(...),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Streaming chat endpoint. Tenant binding is from the authenticated
    user; any caller-supplied tenant hint is ignored. The orchestrator
    runs the tool-use loop, validates emitted actions against the
    site map, and persists session + message rows with cost
    telemetry.
    """
    # Lazy import to avoid module-import-time httpx dependency for
    # the lighter-weight /assistant/config and site-map endpoints.
    from services import assistant_orchestrator
    from services import llm_rate_limit
    from utils import rate_limiter

    # GE.MAA.1 (5.5/10.5) — a session the caller already ended, or one that
    # has aged past its tenant's retention window, must not accept another
    # turn. Checked before anything else so a 410 costs no rate-limit budget
    # and no model-server slot.
    try:
        assistant_orchestrator.check_session_active(db, current_user, body.session_id)
    except assistant_orchestrator.SessionInactiveError as e:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"Assistant session has {e.reason}",
        )

    # GE.MAA.1 (5.6) — the assistant_chat bucket. Separate from, and checked
    # before, the llm_rate_limit concurrency/window ceiling below: that one
    # bounds the shared model server across every LLM tool, this one is a
    # per-route dial for chat alone. See utils/rate_limiter.py's docstring.
    rate_limiter.check_assistant_chat_rate_limit(current_user.tenant_id, current_user.id)

    # managed-ai-surfaces-and-stack-ops §3.1 — the assistant is free, and a
    # volume ceiling rather than a meter is what bounds it.
    #
    # Acquired HERE and not inside the generator, which matters more than it
    # looks: once StreamingResponse starts, the status line is already on the
    # wire, so a refusal raised mid-stream can only be an SSE error event with
    # a 200 next to it. Clients cannot see a 200 as rate limiting, and no
    # Retry-After would reach them. Raising before the response is constructed
    # lets the existing RateLimited handler answer with a real 429.
    #
    # This also takes a concurrency slot for the life of the turn, which is
    # honest: the stream holds a model-server slot the whole time it runs.
    slot = llm_rate_limit.acquire(db, current_user, llm_rate_limit.ASSISTANT_TOOL)

    async def _stream():
        try:
            async for ev in assistant_orchestrator.stream_chat(
                db,
                current_user,
                message=body.message,
                session_id=body.session_id,
                current_page=body.current_page,
                current_workflow_id=body.current_workflow_id,
                current_app=body.current_app,
                # Fold the bounded "tool=token" list back into
                # the {tool: token} map the orchestrator + gate already speak
                # (stream_chat's confirm_tokens kwarg, assistant_orchestrator
                # ctx["confirm_tokens"]). A token with no '=' names a tool
                # with an empty token, which the gate refuses; nothing here
                # mints or validates tokens.
                confirm_tokens=_confirm_token_map(body.confirm_tokens),
            ):
                yield ev
        finally:
            # Runs on normal completion, on error, AND when the client
            # disconnects mid-stream — Starlette closes the generator, which
            # raises GeneratorExit through here. A browser tab closed halfway
            # through an answer is the common case, and without this it would
            # cost that tenant a slot until the lease lapsed.
            llm_rate_limit.release(slot)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            # GY.V10 — AI-interaction notice. Marks that this response
            # was (partly) produced by an AI system so the UI can show
            # its generated-content notice (EU AI Act transparency).
            "X-MOD-AI-Generated": "true",
        },
    )


class _SessionEndRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)


class _SessionEndResponse(BaseModel):
    session_id: str
    ended_at: Optional[str] = None
    already_ended: bool = False


@router.post("/session/end", response_model=_SessionEndResponse)
def end_assistant_session(
    body: _SessionEndRequest = Body(...),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
) -> dict:
    """GE.MAA.1 (5.5) — soft-end the caller's own session. Tenant/user-scoped
    from the authenticated user exactly like /chat: no caller-supplied
    tenant parameter exists, so ending another user's session (even within
    the same tenant) 404s rather than leaking whether the id exists.

    A soft end (sets `ended_at`), never a row delete — the transcript stays
    queryable for audit (ISO 27001). Idempotent: ending an already-ended
    session returns 200 with `already_ended: true` rather than erroring, so
    a client that retries a dropped response can't turn this into a 404.
    """
    from services import assistant_orchestrator

    try:
        return assistant_orchestrator.end_session(db, current_user, body.session_id)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )


class _EscalateRequest(BaseModel):
    session_id: Optional[str] = None
    subject: str
    message: str
    severity: Literal["normal", "emergency"] = "normal"


class _EscalateResponse(BaseModel):
    ok: bool
    recipient: str
    tenant_admin_count: int
    severity: str
    message: str


@router.post("/escalate", response_model=_EscalateResponse)
def escalate(
    body: _EscalateRequest = Body(...),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
) -> _EscalateResponse:
    """User-initiated escalation from the assistant panel's "Contact
    human support" footer link. Mirrors the LLM-driven
    `contact_support` tool but without going through the chat loop —
    direct path for users who want a human without chatting first.
    Both paths converge on the same `escalate_assistant_session`
    helper, so the email shape, audit log, and tenant-admin fan-out
    are identical.

    DELIBERATELY NOT RATE LIMITED (§3.3), and it must stay that way. A user
    who has hit the assistant ceiling is, by definition, someone who kept
    asking and did not get an answer — precisely the person who most needs a
    human. Limiting this endpoint would convert a volume ceiling on a free
    convenience into a support blackout. It costs no model time to run: no LLM
    call happens on this path, so there is nothing here for a limiter to
    protect."""
    from services.assistant_orchestrator import escalate_assistant_session

    result = escalate_assistant_session(
        db=db,
        user=current_user,
        session_id=body.session_id,
        subject=body.subject,
        message=body.message,
        severity=body.severity,
    )
    return _EscalateResponse(
        ok=bool(result.get("ok")),
        recipient=str(result.get("recipient") or ""),
        tenant_admin_count=int(result.get("tenant_admin_count") or 0),
        severity=str(result.get("severity") or "normal"),
        message=str(result.get("message") or ""),
    )
