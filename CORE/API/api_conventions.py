"""Canonical cross-cutting API conventions (openspec: stack-training-and-prewarm, D6).

This module is the ONE place the (future) refactored routers reach for the
cross-cutting concerns the audit found hand-rolled per-router
(docs/api-audit/deep/code-quality-patterns.md §3 unbounded lists, §6 ad-hoc
error shapes, §8 duplicated auth/pagination boilerplate):

    1. A standard error envelope + exception handlers  -> `ErrorEnvelope`,
       `error_response`, `install_error_handlers`.
    2. A standard pagination dependency (limit/offset — the dominant style in
       the existing codebase) + a typed list envelope -> `Pagination`,
       `pagination`, `Page`.
    3. A standard "current user + tenant + permission check" entry point that
       COMPOSES the existing auth (keycloak_auth via dependencies.py) and
       permissions.py — it does NOT reinvent auth -> `authenticated_user`,
       `require_access`, `AccessContext`, `access`.

SCAFFOLD ONLY. Nothing here is wired into main.py yet; per-domain migration
(Gate 2) adopts these, and the lint gate (tools/api_lint.py) checks for them.

Design constraints honoured here:
    * Heavy app modules (`dependencies`, `models`, `permissions`) are imported
      LAZILY inside functions so importing this module has no import-time side
      effects and no circular-import risk — matching the function-local-import
      convention used throughout the codebase.
    * The auth wrapper is a thin composition over the EXISTING dependencies
      (`require_active_and_onboarded_user`, `require_capability`,
      `require_resource_capability`); it adds a canonical name + a tenant-scoped
      context object, nothing more.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, List, Optional, TypeVar, TYPE_CHECKING

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

if TYPE_CHECKING:  # import only for type checkers; no runtime cost / cycle
    import models


# =============================================================================
# 1. Error envelope
# -----------------------------------------------------------------------------
# The audit counted 1,756 `raise HTTPException(...)` sites with inconsistent
# `detail` shapes (str vs dict). The canonical shape below gives every error a
# stable JSON body:
#
#     {"error": {"code": "not_found", "message": "...", "details": {...},
#                "request_id": "..."}}
#
# Routers keep raising plain `HTTPException` (no rewrite needed); the registered
# handlers normalise every response into this envelope.
# =============================================================================


class ErrorDetail(BaseModel):
    """The body of a single API error."""

    code: str = Field(..., description="Stable machine-readable error code, e.g. 'not_found'.")
    message: str = Field(..., description="Human-readable message safe to surface to a client.")
    details: Optional[Any] = Field(
        default=None, description="Optional structured context (field errors, ids, etc.)."
    )
    request_id: Optional[str] = Field(
        default=None, description="Correlation id echoed from the request when available."
    )


class ErrorEnvelope(BaseModel):
    """The canonical top-level error response body. Use as an endpoint's
    `responses={...}` error model so the OpenAPI schema documents the shape."""

    error: ErrorDetail


# Minimal HTTP-status -> stable-code map. Extend as domains adopt the envelope.
_STATUS_CODE_NAMES = {
    400: "bad_request",
    401: "unauthorized",
    402: "payment_required",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "unprocessable_entity",
    429: "rate_limited",
    500: "internal_error",
    502: "bad_gateway",
    503: "unavailable",
    504: "timeout",
}


def code_for_status(status_code: int) -> str:
    """Map an HTTP status code to the canonical stable error `code` string."""
    return _STATUS_CODE_NAMES.get(status_code, f"http_{status_code}")


def error_response(
    status_code: int,
    message: str,
    *,
    code: Optional[str] = None,
    details: Optional[Any] = None,
    request_id: Optional[str] = None,
) -> JSONResponse:
    """Build a JSONResponse carrying the canonical `ErrorEnvelope`.

    Prefer raising `HTTPException` in handlers (the installed handlers wrap it);
    use this helper directly only where you must return a Response object.
    """
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code or code_for_status(status_code),
            message=message,
            details=details,
            request_id=request_id,
        )
    )
    return JSONResponse(status_code=status_code, content=envelope.model_dump())


def _request_id(request: Request) -> Optional[str]:
    """Best-effort correlation id from common headers / request.state."""
    return (
        request.headers.get("X-Request-ID")
        or request.headers.get("X-Correlation-ID")
        or getattr(request.state, "request_id", None)
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Normalise any `HTTPException` into the canonical error envelope.

    `exc.detail` may be a plain string (message) or a dict (already-structured
    context) — both are folded into `ErrorEnvelope` without losing information,
    and the exception's own headers (e.g. `X-Error-Code`) are preserved.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("detail") or "Request failed")
        code = str(detail.get("code") or code_for_status(exc.status_code))
        details: Optional[Any] = detail
    else:
        message = str(detail) if detail is not None else "Request failed"
        code = code_for_status(exc.status_code)
        details = None
    resp = error_response(
        exc.status_code,
        message,
        code=code,
        details=details,
        request_id=_request_id(request),
    )
    if getattr(exc, "headers", None):
        for key, value in exc.headers.items():
            resp.headers[key] = value
    return resp


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all so an unexpected error still returns the canonical envelope
    (with a correlation id) instead of a bare framework 500."""
    return error_response(
        500,
        "An internal error occurred.",
        code="internal_error",
        request_id=_request_id(request),
    )


def install_error_handlers(app) -> None:
    """Register the canonical handlers on a FastAPI app.

    NOT called from main.py yet (scaffold). When the API adopts the envelope,
    `main.py` will call `api_conventions.install_error_handlers(app)` once.
    """
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


# =============================================================================
# 2. Pagination
# -----------------------------------------------------------------------------
# The dominant existing style is `limit`/`offset` (measured in routers/: ~limit
# + offset far outweigh cursor usage), so the canonical dependency matches it.
# Every list endpoint SHALL depend on `pagination` (bounds enforced by Query) so
# no list is unbounded (fixes audit §3: 61 unbounded list endpoints).
# =============================================================================

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

T = TypeVar("T")


@dataclass(frozen=True)
class Pagination:
    """Resolved, bounded pagination window. `limit` is clamped by the Query
    validator (1..MAX_LIMIT); `offset` is >= 0."""

    limit: int
    offset: int

    def apply(self, query):
        """Apply this window to a SQLAlchemy query: `.offset().limit()`."""
        return query.offset(self.offset).limit(self.limit)


def pagination(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Max rows to return."),
    offset: int = Query(0, ge=0, description="Rows to skip."),
) -> Pagination:
    """Canonical pagination dependency (limit/offset).

    Usage:
        @router.get("/things", response_model=Page[ThingOut])
        def list_things(page: Pagination = Depends(pagination), ...):
            items, total = svc.list_things(db, tenant_id, page)
            return Page.build(items, total, page)
    """
    return Pagination(limit=limit, offset=offset)


class Page(BaseModel, Generic[T]):
    """Canonical envelope for a paginated list response.

    Carries the page window back so clients can drive next/prev without guessing
    the server's defaults.
    """

    items: List[T]
    total: int = Field(..., description="Total rows matching the query, ignoring the page window.")
    limit: int
    offset: int

    @classmethod
    def build(cls, items: List[T], total: int, page: "Pagination") -> "Page[T]":
        """Construct a Page from a result slice + total count + the window used."""
        return cls(items=items, total=total, limit=page.limit, offset=page.offset)


# =============================================================================
# 3. Auth + tenant + permission — one canonical entry point
# -----------------------------------------------------------------------------
# This does NOT reinvent auth. It composes the EXISTING chain:
#   keycloak_auth  -> dependencies.get_current_user
#                  -> dependencies.require_active_and_onboarded_user
#   permissions    -> dependencies.require_capability / require_resource_capability
#                     (which call resource_permissions.authorize)
# and adds (a) a canonical name every future router uses and (b) a tenant-scoped
# context object so handlers stop re-reading `current_user.tenant_id` by hand
# (audit §8: manual tenant filter in 15+ files).
# =============================================================================


@dataclass
class AccessContext:
    """The resolved caller for a request: an active, onboarded user plus their
    tenant. Handlers receive this instead of a bare `User` so tenant scoping is
    explicit and uniform."""

    user: "models.User"

    @property
    def tenant_id(self) -> int:
        """The caller's tenant id — the scope EVERY query must filter on."""
        return self.user.tenant_id

    @property
    def user_id(self) -> int:
        return self.user.id


def authenticated_user():
    """Canonical dependency for 'an active, onboarded, rate-limited user'.

    Thin alias over the existing `dependencies.require_active_and_onboarded_user`
    so routers import ONE canonical name. Use as a parameter default:

        def handler(user: models.User = authenticated_user()):
    """
    from dependencies import require_active_and_onboarded_user

    return Depends(require_active_and_onboarded_user)


def require_access(
    resource_type: "models.ResourceType",
    capability: str,
    *,
    resource_id_param: Optional[str] = None,
):
    """Canonical permission dependency — composes the existing RBAC chokepoint.

    Returns a `Depends(...)` that:
      * authenticates + requires active/onboarded (via the existing deps), then
      * enforces `capability` on `resource_type` through
        `resource_permissions.authorize` (the single authorization chokepoint).

    When `resource_id_param` names a path parameter, the row-level check
    (`require_resource_capability`) is used; otherwise the type-level check
    (`require_capability`) is used.

        def handler(user: models.User = require_access(
            models.ResourceType.WORKFLOW, "view", resource_id_param="workflow_id")):
    """
    from dependencies import require_capability, require_resource_capability

    if resource_id_param:
        return Depends(require_resource_capability(capability, resource_type, resource_id_param))
    return Depends(require_capability(resource_type, capability))


def access(
    resource_type: Optional["models.ResourceType"] = None,
    capability: Optional[str] = None,
    *,
    resource_id_param: Optional[str] = None,
):
    """Canonical dependency returning an `AccessContext` (user + tenant).

    With no `resource_type`/`capability` it just requires an authenticated,
    active, onboarded user. With them, it additionally enforces the capability
    before yielding the context.

        def handler(ctx: AccessContext = Depends(access(models.ResourceType.SECRET, "view"))):
            rows = svc.list(db, ctx.tenant_id, page)
    """
    from dependencies import (
        require_active_and_onboarded_user,
        require_capability,
        require_resource_capability,
    )

    if resource_type is not None and capability is not None:
        if resource_id_param:
            guard = require_resource_capability(capability, resource_type, resource_id_param)
        else:
            guard = require_capability(resource_type, capability)
    else:
        guard = require_active_and_onboarded_user

    def dependency(user=Depends(guard)) -> AccessContext:
        return AccessContext(user=user)

    return dependency
