# Worker-Exec MCP

A locked-down MCP server that lets an external AI agent **exec commands on a MOD
worker's host** — and check status, cancel, or wait for results — through your
own LLM. It is a thin adapter over the MOD worker host-exec API
(`/v1/workers/{id}/exec`): it holds no orchestration logic and no server-side
LLM. Every call forwards your API key, so the API's scope, read-only, and tenant
checks apply unchanged.

## Prerequisites

Worker-side host execution is not available in this build. Before the tools below will work:
- The target worker's build/install must actually relay to `mod-host-exec`
  (a worker without this is not host-exec-capable, regardless of settings).
- Platform setting `worker.host_exec_enabled` must be truthy.
- The caller's user must have an access policy grant for the target worker.

Without these, the API rejects every exec attempt with 403.

## Auth

Mint a MOD API key carrying the **`worker:exec`** scope (or broader worker
permissions that include host exec). A key without it (or a read-only key, or
a browser session) is rejected by the API.

## Run

```bash
cd CORE/worker-exec-mcp
pip install -r requirements.txt
MOD_API_URL=https://api.your-mod-instance \
MOD_API_KEY=sk_... \
python -m worker_exec_mcp.server
```

Then point an MCP-capable client (a coding agent) at the stdio server.

## Tools (mechanical only)

| Tool | Does |
|------|------|
| `worker_exec_targets` | List exec-able workers (call this first to pick a `worker_id`) |
| `worker_exec_submit` | Dispatch a command on a worker's host (argv is a list, never a shell string) |
| `worker_exec_status` | DB-backed status of an exec job (status, exit_code, run_as, redacted argv) |
| `worker_exec_cancel` | Cancel a queued/running exec job |
| `worker_exec_wait` | Poll until terminal (succeeded/failed/timeout/cancelled) or timeout |

`argv` is an argument vector — a list of strings. There is **no shell**
invocation; the MCP never hands a shell to the agent. The API-side host helper
execs the command directly.

Running on a worker always uses the worker's execution mode: dedicated workers
are sandboxed, shared workers are always sandboxed. `run_as=root` requires a
human MFA session (fresh re-auth within `worker.host_exec_root_reauth_max_age_s`,
default 300s) plus a platform admin, the owning tenant's admin, or an
`allow_root` grant — **this MCP cannot satisfy that**: it authenticates with
an API key (`X-API-Key`), and the API rejects `run_as=root` outright for any
API-key or service channel, no matter the grant. Root exec is only reachable
from a human browser session, never through this server.

## Per-session caller key (owner Q108=A)

A stdio session acts ONLY with the caller's own credential. Supply it when the
session starts: `MOD_MCP_CALLER_KEY=<the caller's own MOD API key>` (preferred
name). `MOD_API_KEY` is still accepted as the single-caller back-compat name and
must also be the caller's own key — never a platform/service key. With no
caller credential the server refuses every tool call outright (no request is
sent). Keys owned by a service principal (tenant service account, or the
platform service user) are rejected by the API on every fleet router.
