"""MOD MCP gateway login gate (owner decision 11-14 8:35 PM CT).

The gateway must REQUIRE A LOGIN before it serves anything under ``/mcp/*`` —
including the MCP ``tools/list`` handshake — not only before tool calls. An
anonymous request gets 401 ``{"error": "authentication required"}`` and never
reaches a Streamable-HTTP session, so no tool name is ever revealed.

The credential is validated by the API itself, once, over
``GET {MOD_API_URL}/v1/wsip/flags`` — the cheapest route that (a) EXISTS IN CE
(``open-core/manifest.yaml`` classify() of its router file
``CORE/API/routers/wsip_flags.py`` = ``allow``; the old probe path
``/v1/drift/events`` is DENIED, so in CE it 404s and every login is rejected)
and (b) carries the dependency ``dependencies.require_bearer_or_api_key``
(CORE/API/dependencies.py ~1701), the same funnel every fleet tool client
already uses. A positive verdict is cached in memory for 60 s per credential
(SHA-256 of the credential is the key; the raw token is never stored, never
logged, never written to disk).

The credential is forwarded EXACTLY as the client sent it — the API requires
the ``Bearer `` scheme on ``Authorization`` (dependencies.py ~1765 checks
``authorization.startswith("Bearer ")``), so the scheme must never be stripped
from what we forward: bearer callers send ``Authorization: Bearer <token>``,
key callers send ``X-API-Key: <key>``.

Verdicts (fail closed in every other case):
  2xx               -> the API accepted the credential
  401 / 403 / 404 / other -> rejected: 401, no tool names
                      (403 is authenticated-but-forbidden — the API answers
                      403 "Inactive user" for a DEACTIVATED user
                      (dependencies.py ~497), so 403 must NOT count as a login)
  unreachable API   -> 503, never allowed

Importable WITHOUT the ``mcp`` package (starlette + httpx only), so its tests
run on a bare box.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Callable, Optional, Tuple

import httpx

PROBE_PATH = "/v1/wsip/flags"
PROBE_ROUTER = "CORE/API/routers/wsip_flags.py"  # CE-kept (classify() = allow)
CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 256
PROBE_TIMEOUT_SECONDS = 5.0

UNAUTH_BODY = {"error": "authentication required"}
UNREACHABLE_BODY = {"error": "authentication service unavailable"}


def _credential_from_headers(headers) -> Tuple[Optional[str], Optional[str]]:
    """(header_name, credential) for Authorization: Bearer / X-API-Key.

    The credential is the value EXACTLY as the client sent it (the ``Bearer ``
    scheme is kept, nothing is stripped): that is what gets forwarded to the
    API probe. (None, None) when the request carries no credential. The
    credential is never logged and never leaves this process except as the
    probe header.
    """
    for name, value in headers:
        key = name.decode("latin-1").lower() if isinstance(name, bytes) else name.lower()
        raw = value.decode("latin-1") if isinstance(value, bytes) else value
        if key == "authorization":
            if raw.strip().lower().startswith("bearer "):
                if raw.strip():
                    return ("authorization", raw.strip())
        elif key == "x-api-key":
            if raw.strip():
                return ("x-api-key", raw.strip())
    return (None, None)


def _is_gated(path: str) -> bool:
    """True for the MCP mounts (``/mcp`` and ``/mcp/<tool>/...``), never /healthz."""
    return path == "/mcp" or path.startswith("/mcp/")


class _PositiveCache:
    """60 s in-memory positive-verdict cache keyed by SHA-256 of the credential.

    Expired entries are dropped on read AND on write, and the 256-entry cap is
    enforced by dropping the OLDEST (earliest-expiry) entries, so a revoked
    key can never stay valid past 60 s. ``clock`` is a test seam.
    """

    def __init__(
        self,
        ttl: float = CACHE_TTL_SECONDS,
        max_entries: int = CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._clock = clock
        self._expiry: dict[str, float] = {}

    def _key(self, credential: str) -> str:
        return hashlib.sha256(credential.encode("utf-8")).hexdigest()

    def fresh(self, credential: str) -> bool:
        key = self._key(credential)
        exp = self._expiry.get(key)
        if exp is None:
            return False
        if exp <= self._clock():
            del self._expiry[key]
            return False
        return True

    def remember(self, credential: str) -> None:
        now = self._clock()
        self._expiry = {k: v for k, v in self._expiry.items() if v > now}
        while len(self._expiry) >= self._max:
            oldest = min(self._expiry, key=self._expiry.get)
            del self._expiry[oldest]
        self._expiry[self._key(credential)] = now + self._ttl


class LoginGate:
    """ASGI middleware: every request under ``/mcp/*`` must present a valid login."""

    def __init__(
        self,
        wrapped_app,
        *,
        api_base_url: str = "",
        probe_path: str = PROBE_PATH,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        max_entries: int = CACHE_MAX_ENTRIES,
        probe: Optional[Callable[..., Optional[int]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wrapped = wrapped_app
        self._api_base = (api_base_url or "").strip().rstrip("/")
        self._probe_path = probe_path
        self._cache = _PositiveCache(ttl=ttl_seconds, max_entries=max_entries, clock=clock)
        self._probe_fn = probe  # test seam; None -> the real httpx probe
        # Starlette-app introspection stays available (test_gateway.py reads .routes).
        self.routes = getattr(wrapped_app, "routes", [])

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or not _is_gated(scope.get("path", "")):
            await self._wrapped(scope, receive, send)
            return

        header, credential = _credential_from_headers(scope.get("headers") or [])
        if credential is None:
            await _respond(send, 401, UNAUTH_BODY)
            return
        if self._cache.fresh(credential):
            await self._wrapped(scope, receive, send)
            return
        if not self._api_base:
            # MOD_API_URL unset: fail closed, never serve the fleet anonymously.
            await _respond(send, 503, UNREACHABLE_BODY)
            return

        status = await self._verdict(credential, header)
        if status is None:
            await _respond(send, 503, UNREACHABLE_BODY)
            return
        if status in _AUTHENTICATED_STATUSES:
            self._cache.remember(credential)
            await self._wrapped(scope, receive, send)
            return
        await _respond(send, 401, UNAUTH_BODY)

    async def _verdict(self, credential: str, header: str) -> Optional[int]:
        if self._probe_fn is not None:
            return self._probe_fn(credential, header)
        # Forward the caller's credential EXACTLY as sent (scheme included).
        headers = {header: credential} if header else {}
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
                response = await client.get(f"{self._api_base}{self._probe_path}", headers=headers)
                return response.status_code
        except (httpx.HTTPError, ValueError):
            return None

    def _verdict_sync(self, credential: str, header: str) -> Optional[int]:
        """Same verdict path for plain-ASGI callers/tests (no event loop needed)."""
        if self._probe_fn is not None:
            return self._probe_fn(credential, header)
        headers = {header: credential} if header else {}
        try:
            with httpx.Client(timeout=PROBE_TIMEOUT_SECONDS) as client:
                return client.get(f"{self._api_base}{self._probe_path}", headers=headers).status_code
        except (httpx.HTTPError, ValueError):
            return None


_AUTHENTICATED_STATUSES = frozenset({200, 201, 204})  # ONLY 2xx; 403 is NOT a login


async def _respond(send, status_code: int, body: dict) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


def build_login_gate(wrapped_app, api_base_url: str, **kwargs) -> LoginGate:
    """Wire ``wrapped_app`` behind the login gate (used by gateway.build_gateway)."""
    return LoginGate(wrapped_app, api_base_url=api_base_url, **kwargs)
