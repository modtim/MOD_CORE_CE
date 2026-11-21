"""
Rewrites Location headers on trailing-slash redirects (Starlette's
`redirect_slashes`) to be relative, so the browser follows the request's own
scheme+host instead of whatever the ASGI server resolved for that hop.

Root cause (launch-completion-megaplan, dash-permissions:33/69, dash-files:111):
a request that reaches uvicorn WITHOUT the proxy chain (Caddy front ->
nginx, which correctly forward Host + X-Forwarded-Proto) — e.g. a
directly-exposed container port used in dev/E2E, or any future misconfigured
hop — has no forwarded-proto/host info to correct. Starlette's
redirect_slashes middleware then builds an ABSOLUTE Location from that raw
scope, baking in the wrong scheme/host (observed: http://localhost/v1/assets/
instead of https://<api-host>/v1/assets/). Browsers fetch that literal
wrong-origin URL and fail (CORS / connection refused), showing as
"Error loading folders" in the Dashboard.

This is a defense-in-depth, proxy-agnostic fix: even with correct
proxy-header config at every current hop, this generically protects against
any future hop that drops those headers. Scope is deliberately narrow —
only rewrite when the Location differs from the request path by nothing but
a trailing slash — so deliberate absolute redirects issued by routes
(OAuth, external sites) are left untouched.
"""
from typing import Optional
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_REDIRECT_STATUS_CODES = (307, 308)


def relative_location_for_slash_redirect(request_path: str, location: str) -> Optional[str]:
    """Return a relative Location (path[?query]) if `location` differs from
    `request_path` only by a trailing slash, else None (leave untouched)."""
    parts = urlsplit(location)
    if not parts.scheme and not parts.netloc:
        return None  # already relative; nothing to do

    target_path = parts.path
    if target_path == request_path:
        return None  # not actually a slash-only change
    if target_path.rstrip("/") != request_path.rstrip("/"):
        return None  # differs by more than trailing slash — leave alone

    relative = target_path
    if parts.query:
        relative += "?" + parts.query
    return relative


class RelativeSlashRedirectMiddleware(BaseHTTPMiddleware):
    """Rewrite absolute Location headers on trailing-slash redirects to
    relative ones so the browser preserves the request's own origin."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        if response.status_code in _REDIRECT_STATUS_CODES:
            location = response.headers.get("location")
            if location:
                relative = relative_location_for_slash_redirect(request.url.path, location)
                if relative is not None:
                    response.headers["location"] = relative

        return response
