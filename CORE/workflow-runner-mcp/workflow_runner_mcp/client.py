"""Pure HTTP client for the MOD mechanical workflow-run API.

No application logic: every method is a thin call to a public
`/v1/workflow-runner/*` endpoint, carrying the configured API key as
`X-API-Key`. Returns a structured `{status_code, ok, data}` so the MCP tool
layer surfaces API rejections (401/403/404) to the agent unchanged — the MCP
grants no capability the key does not already have.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
import uuid


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
    """A dict with an `ok` convenience — the raw API status + parsed body."""

    @property
    def ok(self) -> bool:  # noqa: D401
        return bool(self.get("ok"))


class WorkflowRunnerClient:
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

    # --- transport -----------------------------------------------------------
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

    def _get(self, path: str) -> ApiResult:
        return self._wrap(self._client.get(self._base + path, headers=self._headers()))

    def _post(self, path: str, body: Dict[str, Any]) -> ApiResult:
        return self._wrap(self._client.post(self._base + path, headers=self._headers(), json=body))

    # --- mechanical operations (1:1 with the public API) ---------------------
    def run_version(self, version_id: int, body: Dict[str, Any]) -> ApiResult:
        return self._post(f"/v1/workflow-runner/versions/{int(version_id)}/run", body)

    def cancel(self, run_id: int) -> ApiResult:
        return self._post(f"/v1/workflow-runner/runs/{int(run_id)}/cancel", {})

    def restart(self, run_id: int, body: Dict[str, Any]) -> ApiResult:
        return self._post(f"/v1/workflow-runner/runs/{int(run_id)}/restart", body)

    def status(self, run_id: int) -> ApiResult:
        return self._get(f"/v1/workflow-runner/runs/{int(run_id)}/status")

    def progress(self, run_id: int) -> ApiResult:
        return self._get(f"/v1/workflow-runner/runs/{int(run_id)}/progress")

    def node_status(self, run_id: int) -> ApiResult:
        return self._get(f"/v1/workflow-runner/runs/{int(run_id)}/node-status")

    def cost(self, run_id: int) -> ApiResult:
        return self._get(f"/v1/workflow-runner/runs/{int(run_id)}/cost")

    # --- exposed-workflow tools (mcpt.a7) -------------------------------------
    def list_exposed_tools(self) -> ApiResult:
        """List the caller-visible exposed-workflow tools (GET /v1/mcp-tools/tools/list)."""
        return self._get("/v1/mcp-tools/tools/list")

    def call_exposed_workflow(self, name: str, arguments: Dict[str, Any],
                              idempotency_key: Optional[str] = None) -> ApiResult:
        """Dispatch an exposed-workflow tool call (POST /v1/mcp-tools/tools/call).

        The idempotency key makes a retry of the same agent tool call land on one
        dispatch; when the agent supplies none we mint a uuid4 here. Neither the
        key nor the arguments are ever logged — the request body goes to the API
        and nothing else sees them.
        """
        body: Dict[str, Any] = {
            "name": name,
            "arguments": arguments,
            "idempotency_key": idempotency_key or str(uuid.uuid4()),
        }
        return self._post("/v1/mcp-tools/tools/call", body)

    def exposed_tools(self) -> List[Dict[str, Any]]:
        """Parsed tool list from list_exposed_tools(); [] on any failure.

        A dead/unreachable list endpoint must never break the static tool set,
        so transport errors are swallowed here (the MCP surface degrades to the
        7 mechanical tools).
        """
        try:
            r = self.list_exposed_tools()
        except Exception:
            return []
        if not r["ok"]:
            return []
        tools = r["data"].get("tools")
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []
