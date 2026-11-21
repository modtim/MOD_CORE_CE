"""Pure HTTP client for the MOD worker host-exec API.

No application logic: every method is a thin call to a public `/v1/workers/*`
endpoint, carrying the configured API key as `X-API-Key`. Returns a structured
`{status_code, ok, data}` so the MCP tool layer surfaces API rejections
(403/404/409) to the agent unchanged -- the MCP grants no capability the key
does not already have. Authorization (who may exec on which worker, in which
mode) lives entirely API-side in services/worker_host_exec_service.py.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

import httpx
import time

# Job-status vocab is the DB CHECK on worker_host_exec_jobs.status
# (ck_worker_host_exec_jobs_status in models.py); the ones below are terminal.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled"})


class _GuardedClient:
    """Refuses a call outright when no caller credential resolves for this
    session -- the request never leaves the process, so the MCP server holds no
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


class ApiResult(dict):
    """A dict with an `ok` convenience -- the raw API status + parsed body."""

    @property
    def ok(self) -> bool:  # noqa: D401
        return bool(self.get("ok"))


class WorkerExecClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = None,
    ):
        if not base_url:
            raise ValueError("base_url is required (e.g. https://api.your-mod-instance)")
        self._base = base_url.rstrip("/")
        self._key = api_key or ""
        self._session_key = ""
        self._sleep = sleep if sleep is not None else time.sleep
        self._client = _GuardedClient(client or httpx.Client(timeout=timeout),
                                      self._resolve_key)

    # --- transport -----------------------------------------------------------
    def _resolve_key(self) -> str:
        """The CALLER's credential for THIS session -- never a service identity.

        Resolution order mirrors the workflow-runner MCP client: (1) an explicit
        per-session caller key, (2) the MCP request-context header, (3)
        MOD_MCP_CALLER_KEY read lazily, (4) the legacy baked key. With nothing
        at all an empty X-API-Key is sent and the API refuses the call.
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
        caller = os.getenv("MOD_MCP_CALLER_KEY", "").strip()
        if caller:
            return caller
        return self._key

    def set_session_caller_key(self, caller_key: str) -> None:
        """Supply the caller's own API key for THIS session (stdio)."""
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
        return self._wrap(self._client.post(self._base + path, headers=self._headers(),
                                            json=body))

    # --- mechanical operations (1:1 with the public API) ---------------------
    def submit(self, worker_id: int, body: Dict[str, Any]) -> ApiResult:
        return self._post(f"/v1/workers/{int(worker_id)}/exec", body)

    def status(self, worker_id: int, job_id: int) -> ApiResult:
        return self._get(f"/v1/workers/{int(worker_id)}/exec/{int(job_id)}")

    def cancel(self, worker_id: int, job_id: int) -> ApiResult:
        return self._post(f"/v1/workers/{int(worker_id)}/exec/{int(job_id)}/cancel", {})

    def targets(self) -> ApiResult:
        return self._get("/v1/host-exec/targets")
