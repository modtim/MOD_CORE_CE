"""Pure HTTP client for the MOD workflow-wizard mechanical-core API."""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx


class _GuardedClient:
    """Refuses a call outright when no caller credential resolves for this
    session — the request never leaves the process, so the MCP server holds no
    ambient authority and can never fall back to a shared/service identity."""

    _REFUSAL = ("No caller credential supplied for this MCP session: supply the "
                "caller's own MOD API key (MOD_MCP_CALLER_KEY at session start, "
                "or X-API-Key/Authorization per connection). No request was sent.")

    def __init__(self, inner, credential):
        self._inner = inner
        self._credential = credential

    def _refused(self) -> httpx.Response:
        return httpx.Response(403, json={"detail": self._REFUSAL})

    def _verb(self, name: str, *args, **kwargs):
        if not self._credential():
            return self._refused()
        return getattr(self._inner, name)(*args, **kwargs)

    def get(self, *a, **k):
        return self._verb("get", *a, **k)

    def post(self, *a, **k):
        return self._verb("post", *a, **k)

    def patch(self, *a, **k):
        return self._verb("patch", *a, **k)

    def put(self, *a, **k):
        return self._verb("put", *a, **k)

    def delete(self, *a, **k):
        return self._verb("delete", *a, **k)


class ApiResult(dict):
    @property
    def ok(self) -> bool:  # noqa: D401
        return bool(self.get("ok"))


class WorkflowAuthoringClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ):
        if not base_url:
            raise ValueError("base_url is required (e.g. https://api.your-mod-instance)")
        self._base = base_url.rstrip("/")
        self._key = api_key or ""
        self._session_key = ""
        self._client = _GuardedClient(client or httpx.Client(timeout=timeout),
                                      self._resolve_key)

    def _resolve_key(self) -> str:
        """The CALLER's credential for THIS session — never a service identity.

        Resolution order is (1) an explicit per-
        session caller key handed to this client instance, (2) the MCP
        request-context header (hosted/streamable-http: the caller's own key
        per connection), (3) MOD_MCP_CALLER_KEY read lazily so a stdio
        session supplies its caller's key at session start, (4) the legacy
        baked key (single-caller stdio back-compat). Nothing here is a
        platform/service credential: the API rejects
        keys owned by a service account outright, and with no credential at
        all an empty X-API-Key is sent so the API refuses the call unchanged.
        """
        if self._session_key:
            return self._session_key
        try:
            from mcp.server.lowlevel.server import request_ctx

            headers = request_ctx.get().request.headers
            hdr = (
                headers.get("x-api-key")
                or headers.get("authorization", "").removeprefix("Bearer ").strip()
            )
            if hdr:
                return hdr
        except Exception:
            pass
        import os

        caller = os.getenv("MOD_MCP_CALLER_KEY", "").strip()
        if caller:
            return caller
        if self._key:
            return self._key
        return ""
        try:
            from mcp.server.lowlevel.server import request_ctx

            headers = request_ctx.get().request.headers
            return (
                headers.get("x-api-key")
                or headers.get("authorization", "").removeprefix("Bearer ").strip()
                or ""
            )
        except Exception:
            return ""

    def set_session_caller_key(self, caller_key: str) -> None:
        """Supply the caller's own API key for THIS session (stdio).
        Called by the server layer when a session starts; takes precedence
        over any baked key so a shared server never acts as one identity."""
        self._session_key = (caller_key or "").strip()

    def _headers(self) -> Dict[str, str]:
        return {"X-API-Key": self._resolve_key(), "Content-Type": "application/json"}

    def _wrap(self, resp: httpx.Response) -> ApiResult:
        try:
            data: Any = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return ApiResult(status_code=resp.status_code, ok=resp.is_success, data=data)

    def _post(self, path: str, body: Dict[str, Any]) -> ApiResult:
        return self._wrap(self._client.post(self._base + path, headers=self._headers(), json=body))

    def apply(self, body: Dict[str, Any]) -> ApiResult:
        return self._post("/v1/workflow-authoring/apply", body)

    def test_run(self, body: Dict[str, Any]) -> ApiResult:
        return self._post("/v1/workflow-authoring/test-run", body)

    def test_run_status(self, run_id: int) -> ApiResult:
        return self._wrap(self._client.get(
            self._base + f"/v1/workflow-authoring/test-run/{int(run_id)}/status",
            headers=self._headers()))

    def queue_missing(self, body: Dict[str, Any]) -> ApiResult:
        return self._post("/v1/workflow-authoring/queue-missing-nodes", body)

    def audit_action(self, body: Dict[str, Any]) -> ApiResult:
        return self._post("/v1/workflow-authoring/audit-action", body)
