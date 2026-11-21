"""The mechanical tool set exposed by the Worker-Host-Exec MCP server.

Each tool is a thin wrapper over a `WorkerExecClient` call. There is
deliberately NO shell: `argv` is an argument vector, never a command string, so
the MCP never hands a shell to the agent -- the API-side host helper execs it
directly. Authorization (which user may exec on which worker, in which mode,
as which uid) is enforced API-side; the MCP grants nothing the key lacks.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .client import TERMINAL_STATUSES, WorkerExecClient

# The complete, fixed tool set. Anything not here is not exposed.
EXEC_TOOL_NAMES: List[str] = [
    "worker_exec_submit",
    "worker_exec_status",
    "worker_exec_cancel",
    "worker_exec_wait",
    "worker_exec_targets",
]

# Guard rails: substrings that must NEVER appear in an exposed tool name -- a
# shell/exec-string capability leaking in would be caught here.
FORBIDDEN_TOOL_SUBSTRINGS: List[str] = [
    "shell", "eval", "script", "author", "synthesize", "generate", "llm",
]

# Client-side validation rejections get this pseudo status so the agent sees a
# rejection in the same shape as an API rejection.
_REJECT_STATUS = 422


def _invalid(detail: str) -> Dict[str, Any]:
    return {"status_code": _REJECT_STATUS, "ok": False,
            "data": {"detail": detail}, "error": detail}


def _surface(result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the server's `detail` as `error` so 403/404/409 reasons reach the
    agent verbatim (the API key itself is never echoed).

    993c: host-exec's 403 `detail` is now a dict ({"error", "code",
    "message", "fix"}) instead of a plain string. Keep `error` a string for
    the agent either way -- pull `detail["error"]` when it's a dict, fall
    back to the raw `detail` string otherwise (404/409 and anything not
    yet migrated to the dict shape).
    """
    if result.get("ok"):
        return result
    data = result.get("data") or {}
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("error")
    out = dict(result)
    out["error"] = detail or f"HTTP {result.get('status_code')}"
    return out


def build_tools(client: WorkerExecClient) -> Dict[str, Callable[..., Dict[str, Any]]]:
    """Bind the tool implementations to a client. Returns {name: callable}."""

    def worker_exec_submit(
        worker_id: int,
        argv: List[str],
        cwd: Optional[str] = None,
        timeout_s: Optional[int] = None,
        run_as: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Dispatch a host-exec job on a worker. `argv` is an argument vector
        (never a shell string). Returns the 202 job handle {job_id, status,
        mode} or the API's rejection (403 not authorized / 404 no worker)."""
        err = _check_worker(worker_id) or _check_argv(argv)
        if err:
            return _invalid(err)
        body: Dict[str, Any] = {"argv": list(argv)}
        if cwd is not None:
            body["cwd"] = cwd
        if timeout_s is not None:
            body["timeout_s"] = timeout_s
        if run_as is not None:
            body["run_as"] = run_as
        return _surface(client.submit(worker_id, body))

    def worker_exec_status(worker_id: int, job_id: int) -> Dict[str, Any]:
        """DB-backed status of a host-exec job (status, exit_code, run_as,
        redacted argv)."""
        err = _check_worker(worker_id) or _check_job(job_id)
        if err:
            return _invalid(err)
        return _surface(client.status(worker_id, job_id))

    def worker_exec_cancel(worker_id: int, job_id: int) -> Dict[str, Any]:
        """Cancel a queued/running host-exec job. Already-terminal jobs are
        rejected by the API with 409 and that detail is surfaced."""
        err = _check_worker(worker_id) or _check_job(job_id)
        if err:
            return _invalid(err)
        result = _surface(client.cancel(worker_id, job_id))
        if result.get("ok"):
            result["cancelled"] = result.get("data", {}).get("status") == "cancelled"
        return result

    def worker_exec_wait(
        worker_id: int,
        job_id: int,
        timeout_s: int = 600,
        poll_s: float = 2.0,
    ) -> Dict[str, Any]:
        """Poll job status until terminal (succeeded/failed/timeout/cancelled)
        or `timeout_s` elapses. Returns the final job dict; on timeout the
        result carries `timed_out: True` plus the last-seen status."""
        err = _check_worker(worker_id) or _check_job(job_id)
        if err:
            return _invalid(err)
        if not isinstance(timeout_s, int) or timeout_s <= 0:
            return _invalid("timeout_s must be a positive integer")
        if not isinstance(poll_s, (int, float)) or poll_s <= 0:
            return _invalid("poll_s must be a positive number")
        waited = 0.0
        last: Dict[str, Any] = {}
        while True:
            last = _surface(client.status(worker_id, job_id))
            if not last.get("ok"):
                return last
            status = (last.get("data") or {}).get("status")
            if status in TERMINAL_STATUSES:
                return last
            if waited >= timeout_s:
                out = dict(last)
                out["timed_out"] = True
                out["waited_s"] = waited
                return out
            step = min(float(poll_s), max(0.0, timeout_s - waited))
            client._sleep(step)
            waited += step

    def worker_exec_targets(
        online_only: bool = True,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List the workers this caller may host-exec on -- call this FIRST to
        pick a `worker_id` for `worker_exec_submit`. Filters: `online_only` drops
        workers whose last heartbeat shows they are offline; `mode` narrows to
        one exec mode ("direct" or "sandboxed"). Returns the surviving rows plus
        `count`; API rejections (403/404) are surfaced unchanged."""
        err = _check_mode(mode)
        if err:
            return _invalid(err)
        result = _surface(client.targets())
        if not result.get("ok"):
            return result
        rows = (result.get("data") or {}).get("targets")
        if not isinstance(rows, list):
            return {"status_code": result.get("status_code"), "ok": False,
                    "data": result.get("data"),
                    "error": "malformed targets response"}
        kept = [row for row in rows
                if (not online_only or row.get("online"))
                and (mode is None or row.get("mode") == mode)]
        out = dict(result)
        out["targets"] = kept
        out["count"] = len(kept)
        return out

    def _check_worker(worker_id: Any) -> Optional[str]:
        if isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id <= 0:
            return "worker_id must be a positive integer"
        return None

    def _check_mode(mode: Any) -> Optional[str]:
        if mode is not None and mode not in ("direct", "sandboxed"):
            return "mode must be 'direct' or 'sandboxed'"
        return None

    def _check_job(job_id: Any) -> Optional[str]:
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
            return "job_id must be a positive integer"
        return None

    def _check_argv(argv: Any) -> Optional[str]:
        if isinstance(argv, str):
            return ("argv must be an argument vector (list of str), not a shell "
                    "string -- this MCP never invokes a shell")
        if not isinstance(argv, list) or not argv:
            return "argv must be a non-empty list of str"
        if any(not isinstance(a, str) for a in argv):
            return "every argv element must be a str"
        return None

    tools: Dict[str, Callable[..., Dict[str, Any]]] = {
        "worker_exec_targets": worker_exec_targets,
        "worker_exec_submit": worker_exec_submit,
        "worker_exec_status": worker_exec_status,
        "worker_exec_cancel": worker_exec_cancel,
        "worker_exec_wait": worker_exec_wait,
    }
    # Invariant: exactly the exec tool set, nothing else.
    assert set(tools) == set(EXEC_TOOL_NAMES)
    for name in tools:
        if any(s in name for s in FORBIDDEN_TOOL_SUBSTRINGS):
            raise AssertionError(f"forbidden capability in tool name {name}")
    return tools
