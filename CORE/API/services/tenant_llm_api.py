"""GX.S5 (launch-completion-megaplan Group X) — tenant LLM endpoint.

An OpenAI-compatible chat-completions endpoint for tenants:

    POST /v1/llm/chat/completions    (CE; `stream: true` via SSE passthrough)
    GET  /v1/llm/models              list the models this tenant can use

The `model` in a request names a model the tenant's own Persistent
Stacks serve (resolved through the SAME machinery every other LLM
consumer uses: `llm_idle.served_models` for the served names,
`routers.ai_config._stack_custom_base_url` for the reachable base URL —
no second resolver written here).

Edition seam (GX.S0): the single-model endpoint is CE. METERING +
quotas are metered, resolved through the existing `usage_metering`
capability (the same entitlement axis `dependencies.require_entitlement`
and services.email_send_api reuse) — no new capability invented. CE
tenants still get the request-size + per-minute rate limits, just not
the monthly-token quota or the per-call usage record.

Usage records are METADATA ONLY (model, engine-reported token counts,
latency, status) — prompt / completion text is NEVER persisted.
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from utils.http_errors import internal_http_error

logger = logging.getLogger(__name__)

# PAID seam — the capability that entitles metering + quotas. Reused from
# the entitlement catalogue rather than inventing a new one: `usage_metering`
# is the platform's "metered usage logging" axis, exactly what S5 metering
# is. CE tenants route + serve fine but are not metered/quotas'd.
METERING_CAPABILITY = "usage_metering"

# Backend call timeout (seconds). Long enough for a full local-LLM
# generation, short enough that a dead backend fails the request instead
# of hanging the worker thread.
_BACKEND_TIMEOUT_S = 180.0

#: FX.151 p4b — the tenant whose SSRF allowlist applies to the current
#: backend call. Set by the entry points (`chat_completion` /
#: `chat_completion_stream`) and read by the `_http_post` seam, so the seam's
#: test-contract signature stays `(url, json, stream)`. Context-local, so two
#: concurrent requests for different tenants never share an allowlist.
_request_tenant_id: "contextvars.ContextVar[Optional[int]]" = (
    contextvars.ContextVar("fx151_p4b_tenant_llm_tenant_id", default=None))


def get_tenant(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    """The tenant row (used for the entitlement check). Routers stay free of
    raw `db.query` — this is the service-side accessor."""
    return db.query(models.Tenant).filter(
        models.Tenant.id == tenant_id).first()


def has_metering(db: Session, tenant: Optional[models.Tenant]) -> bool:
    """Metered seam — True when the tenant's license entitles LLM metering +
    quotas (the monthly-token quota, per-call usage rows). CE tenants get
    routing + size/rate limits only. Fail-open to False on resolver errors
    (same posture as services.email_send_api.has_delivery_tracking)."""
    from utils import entitlements
    try:
        es = entitlements.get_entitlements(db, tenant)
        return entitlements.evaluate_feature(
            METERING_CAPABILITY, es).allowed
    except Exception as e:  # noqa: BLE001
        logger.warning("tenant_llm_api: entitlement check failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Platform settings (DB-backed, group "llm" — registered in
# services/platform_settings.py). Never env.
# ---------------------------------------------------------------------------

def _ce_quota_unlimited() -> bool:
    """CE only: the monthly LLM token quota is UNLIMITED (owner 11-13 13:33).

    The CE entitlement stub grants `usage_metering` (only HA_CONTROL_PLANE is
    denied), so `has_metering` is True in CE and a finite
    `llm.monthly_token_quota` platform SETTING would otherwise arm a real quota
    for a CE user. Same posture as utils/rate_limiter._ce_raise_plan_limit:
    lazy import of the shared edition probe, an erroring probe is PAID.
    """
    try:
        from utils.paid_import import is_ce_edition
        return bool(is_ce_edition())
    except Exception:  # noqa: BLE001
        return False


def _limits(db: Session, tenant_id: Optional[int]) -> Dict[str, Any]:
    from services import platform_settings
    return {
        "max_request_bytes": int(platform_settings.get_setting(
            "llm.max_request_bytes", db, tenant_id)),
        "rate_per_minute": int(platform_settings.get_setting(
            "llm.rate_limit_per_minute", db, tenant_id)),
        "monthly_token_quota": int(platform_settings.get_setting(
            "llm.monthly_token_quota", db, tenant_id)),
    }


def _rate_limited(tenant_id: int, limits: Dict[str, Any]) -> bool:
    """True when the tenant is over the per-minute cap. Uses the shared
    Redis sliding-window limiter (utils.rate_limiter); a Redis outage
    degrades to open (same posture as every other limiter)."""
    from utils.rate_limiter import check_rate_limit
    # FIX-CE-UNLIMITED-B1: <= 0 is the CE "unlimited" sentinel.
    if int(limits["rate_per_minute"]) <= 0:
        return False
    ok, _remaining, _reset = check_rate_limit(
        f"tenant_llm:{tenant_id}",
        max_requests=limits["rate_per_minute"],
        window_seconds=60,
        raise_on_limit=False,
    )
    return not ok


def _month_tokens_used(db: Session, tenant_id: int) -> int:
    """Total (prompt+completion) tokens this tenant used this calendar
    month, from the metering records. 0 for CE (they write no rows)."""
    now = datetime.now(timezone.utc)
    since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = db.query(func.coalesce(
        func.sum(models.TenantLlmCall.prompt_tokens
                 + models.TenantLlmCall.completion_tokens), 0)).filter(
        models.TenantLlmCall.deleted_at.is_(None),
        models.TenantLlmCall.tenant_id == tenant_id,
        models.TenantLlmCall.created_at >= since,
    ).one()
    return int(row[0] or 0)


# ---------------------------------------------------------------------------
# Model resolution — reuse the existing LLM machinery, never a second copy.
# ---------------------------------------------------------------------------

def _stack_served_models(stack) -> List[str]:
    # 2400GX-ceLLM — served names come from services.stack_model_endpoint, a
    # CE-allowed module. llm_idle is no longer a PAID seam for this.
    from services.stack_model_endpoint import served_models
    try:
        return served_models(stack)
    except Exception as e:  # noqa: BLE001
        logger.debug("served_models failed for stack %s: %s",
                     getattr(stack, "id", "?"), e)
        return []


def _stack_base_url(stack, db: Session) -> Optional[str]:
    # 2400GX-ceLLM — the CE shim now resolves on BOTH editions: utils.tool_config
    # .stack_custom_base_url calls services.stack_model_endpoint directly.
    from utils.tool_config import stack_custom_base_url
    try:
        return stack_custom_base_url(stack, db)
    except Exception as e:  # noqa: BLE001
        logger.debug("base_url failed for stack %s: %s",
                     getattr(stack, "id", "?"), e)
        return None


def _model_status(st) -> str:
    """30.6.7 c7-a4 — derived model status for the models list:
      * running — the stack is actively serving (`stack_wake._active_statuses()`,
        the same ACTIVE_STACK_STATUSES `_derive_stack_state` in
        routers/persistent_stacks/_shared.py:3612 uses; that router helper is
        NOT imported — routers -> services would be a layering inversion);
      * waking  — desired_state == "running" but not yet serving;
      * sleeping — off / stopped. Lead vet: the overlay's `waiting` state
        (off with a task held for capacity) maps here too, because no wake is
        in flight;
      * stopped — any lookup failure (never raises)."""
    try:
        from services import stack_wake
        status = getattr(st, "status", None)
        desired = getattr(st, "desired_state", None)
        if status in set(stack_wake._active_statuses()):
            return "running"
        if desired == "running":
            return "waking"
        return "sleeping"
    except Exception as e:  # noqa: BLE001
        logger.debug("tenant_llm_api: model status derivation failed: %s", e)
        return "stopped"


def list_models(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """Every model this tenant can call: the union of served model names
    across the tenant's Persistent Stacks (any status — a stopped stack is
    still listed so the app knows its own model names; the call itself 503s
    while the stack is not running)."""
    from crud.persistent_stacks_domain import list_stacks_by_tenant
    # Persistent Stacks CRUD is CE (GT.D5) — no PAID seam here.
    stacks = list_stacks_by_tenant(db, tenant_id)
    out: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for st in stacks:
        for name in _stack_served_models(st):
            entry = seen.get(name)
            if entry is None:
                entry = {"id": name, "object": "model", "owned_by": "mod"}
                seen[name] = entry
                out.append(entry)
            entry["stack_id"] = st.id
            entry["stack_name"] = st.name
            entry["stack_status"] = st.status
            entry["status"] = _model_status(st)
            entry["keep_running"] = bool(getattr(st, "keep_running", False))
    return out


def _resolve_model(db: Session, tenant_id: int,
                   model: str) -> Tuple[Optional[str], str, List[str]]:
    """Resolve `model` to (base_url, model_name) for the tenant.

    Returns (base_url, model_name, available). `base_url` is None when the
    model is known (some stack serves it) but no reachable endpoint is
    available right now — the caller 503s. When NO stack serves it, returns
    (None, model, available) with base_url None AND model not in available —
    the caller 404s. `available` is the tenant's model list for the error."""
    from crud.persistent_stacks_domain import list_stacks_by_tenant
    # Persistent Stacks CRUD is CE (GT.D5) — no PAID seam here.
    stacks = list_stacks_by_tenant(db, tenant_id)
    available = [m["id"] for m in list_models(db, tenant_id)]
    for st in stacks:
        served = _stack_served_models(st)
        if model not in served:
            continue
        # A stopped stack has no live endpoint — a stale mesh entry must not
        # be dialed; report it as "model known, backend not up" (503 path).
        if st.status != "running":
            return None, model, available
        base_url = _stack_base_url(st, db)
        return base_url, model, available
    return None, model, available


def _record_usage(db: Session, *, tenant_id: int, user_id: Optional[int],
                  app_id: Optional[int], model: str,
                  usage: Optional[Dict[str, Any]], latency_ms: int,
                  status: str) -> None:
    """Best-effort metering row (PAID seam only). METADATA ONLY — never
    prompt/completion text. Swallows its own errors: losing a usage row is a
    reporting gap, not an outage; it must never fail a successful call."""
    try:
        u = usage or {}
        pt = u.get("prompt_tokens", 0) or 0
        ct = u.get("completion_tokens", 0) or 0
        row = models.TenantLlmCall(
            tenant_id=tenant_id,
            app_id=app_id,
            user_id=user_id,
            model=model,
            prompt_tokens=int(pt),
            completion_tokens=int(ct),
            latency_ms=latency_ms,
            status=status,
        )
        db.add(row)
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("tenant_llm_api: usage record failed: %s", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _precheck(db: Session, *, tenant_id: int, tenant, body: dict) -> Dict[str, Any]:
    """CE + PAID shared pre-flight: request size, per-minute rate, and the
    PAID-only monthly token quota. Raises 413/429 on breach. Returns the
    limits bundle (reused by the caller for metering)."""
    limits = _limits(db, tenant_id)

    size = len(json.dumps(body).encode("utf-8"))
    if size > limits["max_request_bytes"]:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Request body too large ({size} bytes); the platform "
                   f"limit is {limits['max_request_bytes']} bytes.",
        )
    if _rate_limited(tenant_id, limits):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="LLM rate limit exceeded. Try again shortly.",
            headers={"Retry-After": "60",
                     "X-RateLimit-Limit": str(limits["rate_per_minute"])},
        )
    # PAID only — CE is not metered/quotas'd (no usage rows, no quota).
    # FIX-CE-TENANT-LLM-API-QUOTA (owner 11-13 13:33): CLAMP to unlimited in CE.
    # The CE entitlement stub grants `usage_metering`, so `has_metering` is True
    # there; without this clamp a finite `llm.monthly_token_quota` platform
    # SETTING would arm a real quota for a CE user. Paid is unchanged (the probe
    # is False there, and an erroring probe is paid).
    limits["metered"] = has_metering(db, tenant)
    if (limits["metered"] and not _ce_quota_unlimited()
            and limits["monthly_token_quota"] > 0):
        used = _month_tokens_used(db, tenant_id)
        if used >= limits["monthly_token_quota"]:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"LLM token usage limit reached for this period. "
                       f"({used}/{limits['monthly_token_quota']} tokens).",
                headers={"Retry-After": "3600"},
            )
    return limits


# ---------------------------------------------------------------------------
# The backend call — passthrough to the stack's OpenAI-compatible endpoint.
# ---------------------------------------------------------------------------

def _backend_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/v1/chat/completions"


def _node_name_from_url(base_url: str) -> str:
    """Extract a human-readable node/stack identifier from the base URL."""
    try:
        from urllib.parse import urlparse
        host = urlparse(base_url).hostname or "unknown"
        return host
    except Exception:  # noqa: BLE001
        return "unknown"


def _request_body_bytes(body: dict) -> int:
    """JSON-serialised body size in bytes. Safe: no secrets in the body dict."""
    try:
        return len(json.dumps(body).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return 0


def _audit_after_call(db: Session, tenant_id: int, base_url: str,
                      model: str, body: dict, response_bytes: int) -> None:
    """FX.149.p4 — best-effort audit after a completed backend call.

    The audit row is written in its OWN short-lived session (see
    _audit_ai_call); the caller's session is never touched, so `db` is
    unused here on purpose.
    """
    _audit_ai_call(
        tenant_id=tenant_id, model=model,
        request_bytes=_request_body_bytes(body),
        response_bytes=response_bytes,
        key_type=_key_type(base_url),
        node_name=_node_name_from_url(base_url),
        provider=_extract_provider(body),
    )


def _http_post(url: str, body: dict, stream: bool = False):
    """Single HTTP seam for the backend call — the ONLY place this service
    touches the network. Tests monkeypatch this one function (never a real
    GPU call). Returns the requests.Response (caller owns it).

    GSEC FX.151 p1c — external (public) backend URLs route through the SSRF
    guard; internal/loopback Persistent-Stack addresses (on-prem stacks,
    MagicDNS) stay on the direct client so local GPU stacks keep working.

    GSEC FX.151 p4b — the tenant making the call is handed to the guard so
    that tenant's own `security.ssrf_allowlist` (tenant-scope row, FX.151 p3)
    joins the effective allowlist for THIS request. The seam's signature is
    the test contract (tests patch it with a fixed `(url, json, stream)`
    fake), so the tenant travels in the per-request context var set by the
    entry points below rather than as a new parameter.
    """
    from utils import egress_policy, safe_http
    if egress_policy.url_is_external(url):
        return safe_http.post(url, json=body, stream=stream,
                              tenant_id=_request_tenant_id.get(),
                              _ssrf_connect_timeout=_BACKEND_TIMEOUT_S,
                              _ssrf_read_timeout=_BACKEND_TIMEOUT_S)
    import requests
    return requests.post(url, json=body, timeout=_BACKEND_TIMEOUT_S,
                         stream=stream)


# ---------------------------------------------------------------------------
# FX.149.p4 — audit row per external AI call (append-only, never fails the
# call). Fields: provider, tenant, node, bytes, key_type (platform | byok).
# ---------------------------------------------------------------------------

_PROVIDER_ALIASES: Dict[str, str] = {
    "openai_compatible": "openai",
    "anthropic_compatible": "anthropic",
    "google_compatible": "google",
    "cohere_compatible": "cohere",
    "groq_compatible": "groq",
    "mistral_compatible": "mistral",
    "bedrock_compatible": "bedrock",
    "vertex_compatible": "vertex",
}


def _extract_provider(body: dict) -> str:
    """Best-effort provider name from the request body or model name.

    Only metadata is logged — never prompt content, keys or tokens.
    """
    # Direct provider field if the caller set one.
    provider = (body.get("provider") or "").strip()
    if provider:
        return _PROVIDER_ALIASES.get(provider.lower(), provider)

    # Infer from model name (e.g. "gpt-4o", "claude-3-5-sonnet", etc.)
    model = (body.get("model") or "").lower()
    if any(k in model for k in ("gpt-", "o1-", "o3-", "chatgpt-")):
        return "openai"
    if any(k in model for k in ("claude-", "anthropic")):
        return "anthropic"
    if any(k in model for k in ("gemini-", "groq-", "llama-", "mistral-", "codestral-")):
        return "google" if "gemini" in model else "other"
    return "unknown"


def _key_type(url: str) -> str:
    """Platform key for local/internal endpoints; BYOK for public providers."""
    from utils import egress_policy
    return "byok" if egress_policy.url_is_external(url) else "platform"


def _audit_ai_call(*, tenant_id: int, model: str,
                   request_bytes: int, response_bytes: int,
                   key_type: str, node_name: Optional[str],
                   provider: str) -> None:
    """Append-only audit row, written in its OWN short-lived session.

    Never raises — the AI call must always succeed regardless of whether the
    audit write lands. The audit is a side channel: it must NOT reuse the
    caller's session, because committing there would flush the caller's
    half-done work early and a rollback on a failed audit write would
    discard that pending work (and on the stream path the caller's session
    may already be closed). Mirrors the side-channel pattern used by
    llm_metering / audit_archive: open SessionLocal(), commit, close.
    """
    try:
        from database import SessionLocal
        audit_db = SessionLocal()
    except Exception as e:  # noqa: BLE001
        logger.warning("FX.149 audit session unavailable for %s/%s: %s",
                       tenant_id, model, e)
        return
    try:
        row = models.AuditEvent(
            tenant_id=tenant_id,
            event_name="external_ai_call",
            details={
                "provider": provider,
                "model": model,
                "node": node_name or "unknown",
                "request_bytes": request_bytes,
                "response_bytes": response_bytes,
                "key_type": key_type,
            },
        )
        audit_db.add(row)
        audit_db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("FX.149 audit write failed for %s/%s: %s",
                       tenant_id, model, e)
        try:
            audit_db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            audit_db.close()
        except Exception:  # noqa: BLE001
            pass


def _down_detail(model: str, base_url: str, exc: Optional[str] = None) -> str:
    d = (f"Model '{model}' is not reachable right now "
         f"(endpoint {base_url or 'unresolved'} is down or has no route). "
         f"Start its stack and retry.")
    if exc:
        d += f" Last error: {exc}"
    return d


# ---------------------------------------------------------------------------
# 30.6.7 (c7-a3) — wake-on-demand (CE)
# A request for a model the tenant's OWN stopped stack serves wakes that stack
# through services/stack_wake.ensure_stack_ready, waits inline for at most
# `llm.model_wake_inline_wait_seconds` (DB-backed, never a header/body knob),
# then answers 503 `model_waking` + Retry-After. The CALLER node (c7-a5) owns
# the total `llm.model_wake_timeout_seconds` budget — a request is never held
# that long. No stack/host/base_url internals ever reach the 503.
# ---------------------------------------------------------------------------

# In-flight wake waiters (S2: bounded). A module-level lock + counter so a
# stopped stack can never pile up request threads; the counter is released in
# `finally` on EVERY path (including exceptions).
_WAKE_LOCK = threading.Lock()
_wake_waiters = 0


def _retry_after_s(value: Any, default: int) -> str:
    """Retry-After seconds as a string, clamped to 1..60."""
    try:
        v = int(value)
    except Exception:  # noqa: BLE001
        v = default
    return str(min(60, max(1, v)))


def _serving_stack(db: Session, tenant_id: int, model: str):
    """The CALLER's own stack that serves `model`, or None. Same lookup
    machinery as `_resolve_model` (`list_stacks_by_tenant` + `_stack_served_models`)
    so the wake targets exactly the row the resolver would have used — and
    only the caller's tenant (S1: tenant isolation)."""
    from crud.persistent_stacks_domain import list_stacks_by_tenant
    for st in list_stacks_by_tenant(db, tenant_id):
        if model in _stack_served_models(st):
            return st
    return None


def _wake_headers(db: Session, tenant_id: int, retry_after: str) -> Dict[str, str]:
    """Headers for a `model_waking` 503 (30.6.7 c7-a4, Lead 10-27).

    Besides Retry-After, every such 503 carries
    `X-Model-Wake-Budget-Seconds: <llm.model_wake_timeout_seconds>` — the TOTAL
    budget the caller node (c7-a5 `stack-model-call`) may spend waiting across
    retries. Without this header that platform setting would be dead config:
    the node's `MODEL_WAKE_TIMEOUT_SECONDS` var is optional, and when unset it
    reads this header (else 600). Never raises: a settings lookup failure just
    omits the header, and the node falls back to its own 600."""
    headers = {"Retry-After": retry_after}
    try:
        from services import platform_settings
        budget = platform_settings.get_setting(
            "llm.model_wake_timeout_seconds", db=db)
        headers["X-Model-Wake-Budget-Seconds"] = str(int(budget))
    except Exception as e:  # noqa: BLE001
        logger.debug("tenant_llm_api: wake budget lookup failed: %s", e)
    return headers


def _wake_or_503(db: Session, *, tenant_id: int,
                 user_id: Optional[int] = None, model: str) -> str:
    """Wake the tenant's stopped model stack and return a reachable base_url.

    Runs ONLY in the "model known but base_url is None" branch. Returns the
    base_url once the stack is up (the caller then dials the backend exactly
    as before). Raises 503 otherwise: the OLD "start its stack" detail when
    nothing can be woken / the wake itself failed (exception text logged,
    never returned — S5), and `model_waking` + Retry-After when the wake was
    issued but the stack is not up inside the inline window (held / cap /
    deadline). Exactly ONE `ensure_stack_ready` per request (S2)."""
    global _wake_waiters
    from services import platform_settings
    from services.stack_wake import StackRef, ensure_stack_ready

    inline_wait = int(platform_settings.get_setting(
        "llm.model_wake_inline_wait_seconds", db, tenant_id))
    cap = int(platform_settings.get_setting(
        "llm.wake_wait_max_concurrent", db, tenant_id))

    row = _serving_stack(db, tenant_id, model)
    if row is None:
        # Nothing of THIS tenant serves it — the pre-c7-a3 503, unchanged.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_down_detail(model, None))

    with _WAKE_LOCK:
        # FIX-CE-UNLIMITED-LLM-WAKE-WAIT: cap 0 = no cap (never refuses).
        if cap > 0 and _wake_waiters >= cap:
            over_cap = True
        else:
            _wake_waiters += 1
            over_cap = False
    if over_cap:
        # S2: capacity is the issue — do NOT wake, answer at once.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="model_waking",
            headers=_wake_headers(db, tenant_id,
                                  _retry_after_s(inline_wait, 1)))

    try:
        ref = StackRef(key=row.name, stack_name=row.name,
                       tenant_id=tenant_id, family="llm")
        try:
            res = ensure_stack_ready(db, ref, actor=user_id, wait=False)
            db.commit()  # the wake may have written rows
        except Exception as e:  # noqa: BLE001
            logger.warning("tenant_llm_api: wake of stack %s failed: %s",
                           getattr(row, "id", "?"), e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_down_detail(model, None)) from None

        if getattr(res, "held", False):
            # Won't fit: capacity, not a failure. Next call retries the wake.
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="model_waking",
                headers=_wake_headers(db, tenant_id, "60"))

        if inline_wait <= 0:
            # 0 = never wait inline: wake, then tell the caller to retry.
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="model_waking",
                headers=_wake_headers(db, tenant_id,
                                      _retry_after_s(10, 10)))

        deadline = time.monotonic() + inline_wait  # computed ONCE
        while time.monotonic() < deadline:
            db.rollback()  # S3: no DB transaction open across a sleep
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))
            db.expire_all()
            base_url, _name, _avail = _resolve_model(db, tenant_id, model)
            if base_url is not None:
                return base_url

        # Deadline passed: the stack is waking (or was woken elsewhere) but not
        # reachable yet. `EnsureResult` (stack_wake.py:200) carries no ETA
        # field (ready/woke/held/stack_id/host/reason), so the retry hint is
        # the fixed 10 s, clamped.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="model_waking",
            headers=_wake_headers(db, tenant_id,
                                  _retry_after_s(10, 10)))
    finally:
        with _WAKE_LOCK:
            _wake_waiters -= 1


def chat_completion(db: Session, *, tenant_id: int, user_id: Optional[int],
                    app_id: Optional[int], tenant, body: dict) -> dict:
    """Non-streaming chat completion (CE). Returns the backend's OpenAI
    response dict (choices + usage). Unknown model -> 404 (lists available);
    known-but-down model -> 503 (names the model)."""
    model = body.get("model")
    if not model:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="model is required.")
    base_url, _model_name, available = _resolve_model(db, tenant_id, model)
    if base_url is None and model not in available:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "model not found", "model": model,
                    "available_models": available})
    if base_url is None:
        # 30.6.7 c7-a3: wake the tenant's own stopped stack, wait inline for
        # at most llm.model_wake_inline_wait_seconds, else 503 model_waking.
        # In the stream path this runs before any byte is yielded.
        base_url = _wake_or_503(db, tenant_id=tenant_id, user_id=user_id,
                                model=model)

    _request_tenant_id.set(tenant_id)  # FX.151 p4b: this tenant's allowlist
    limits = _precheck(db, tenant_id=tenant_id, tenant=tenant, body=body)
    start = time.monotonic()
    try:
        resp = _http_post(_backend_url(base_url), body)
    except Exception as e:  # noqa: BLE001
        if limits.get("metered"):
            _record_usage(db, tenant_id=tenant_id, user_id=user_id,
                          app_id=app_id, model=model, usage=None,
                          latency_ms=0, status="failed")
        raise internal_http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "Model backend unavailable", e) from e
    latency = int((time.monotonic() - start) * 1000)
    try:
        if resp.status_code >= 400:
            resp_bytes = int(resp.headers.get("content-length", 0) or 0)
            if limits.get("metered"):
                _record_usage(db, tenant_id=tenant_id, user_id=user_id,
                              app_id=app_id, model=model, usage=None,
                              latency_ms=latency, status="failed")
            _audit_after_call(db, tenant_id, base_url, model, body, resp_bytes)
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=f"Model backend returned {resp.status_code} for "
                       f"'{model}'.")
        data = resp.json()
        resp_bytes = int(resp.headers.get("content-length", 0) or 0)
        if not resp_bytes:
            resp_bytes = len(json.dumps(data).encode("utf-8")) if data else 0
    finally:
        resp.close()
    if limits.get("metered"):
        _record_usage(db, tenant_id=tenant_id, user_id=user_id, app_id=app_id,
                      model=model, usage=data.get("usage"),
                      latency_ms=latency, status="success")
    _audit_after_call(db, tenant_id, base_url, model, body, resp_bytes)
    return data


def chat_completion_stream(db: Session, *, tenant_id: int, user_id: Optional[int],
                           app_id: Optional[int], tenant, body: dict):
    """Streaming chat completion (CE) — SSE passthrough. Yields the backend's
    raw `text/event-stream` chunks. `stream: true` is forced onto the body.
    Metering (PAID) records a row on stream end with whatever `usage` the
    final chunk carried (OpenAI only sends it when `stream_options` asks for
    it; absent -> 0 tokens, which is an acceptable best-effort gap)."""
    model = body.get("model")
    if not model:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="model is required.")
    base_url, _model_name, available = _resolve_model(db, tenant_id, model)
    if base_url is None and model not in available:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "model not found", "model": model,
                    "available_models": available})
    if base_url is None:
        # 30.6.7 c7-a3: wake the tenant's own stopped stack, wait inline for
        # at most llm.model_wake_inline_wait_seconds, else 503 model_waking.
        # In the stream path this runs before any byte is yielded.
        base_url = _wake_or_503(db, tenant_id=tenant_id, user_id=user_id,
                                model=model)

    _request_tenant_id.set(tenant_id)  # FX.151 p4b: this tenant's allowlist
    limits = _precheck(db, tenant_id=tenant_id, tenant=tenant, body=body)
    body = dict(body)
    body["stream"] = True
    start = time.monotonic()
    try:
        resp = _http_post(_backend_url(base_url), body, stream=True)
    except Exception as e:  # noqa: BLE001
        if limits.get("metered"):
            _record_usage(db, tenant_id=tenant_id, user_id=user_id,
                          app_id=app_id, model=model, usage=None,
                          latency_ms=0, status="failed")
        raise internal_http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "Model backend unavailable", e) from e

    if resp.status_code >= 400:
        resp_bytes = int(resp.headers.get("content-length", 0) or 0)
        resp.close()
        if limits.get("metered"):
            _record_usage(db, tenant_id=tenant_id, user_id=user_id,
                          app_id=app_id, model=model, usage=None,
                          latency_ms=0, status="failed")
        _audit_after_call(db, tenant_id, base_url, model, body, resp_bytes)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"Model backend returned {resp.status_code} for "
                   f"'{model}'.")

    _stream_bytes: List[int] = [0]  # mutable accumulator for generator closure

    def _gen():
        usage: Dict[str, Any] = {}
        st = "success"
        try:
            for chunk in resp.iter_content(chunk_size=None):
                if chunk:
                    _stream_bytes[0] += len(chunk)
                    # Best-effort: pull `usage` from any chunk that carries it.
                    try:
                        txt = chunk.decode("utf-8", "ignore")
                        if "data:" in txt:
                            for line in txt.splitlines():
                                line = line.strip()
                                if line.startswith("data:"):
                                    payload = line[5:].strip()
                                    if payload and payload != "[DONE]":
                                        try:
                                            d = json.loads(payload)
                                        except Exception:  # noqa: BLE001
                                            continue
                                        u = d.get("usage")
                                        if isinstance(u, dict):
                                            usage.update(u)
                    except Exception:  # noqa: BLE001
                        pass
                    yield chunk
        except Exception:  # noqa: BLE001
            st = "failed"
            raise
        finally:
            resp.close()
            latency = int((time.monotonic() - start) * 1000)
            if limits.get("metered"):
                _record_usage(db, tenant_id=tenant_id, user_id=user_id,
                              app_id=app_id, model=model, usage=usage,
                              latency_ms=latency, status=st)
            _audit_after_call(db, tenant_id, base_url, model, body,
                              _stream_bytes[0])

    return _gen()
