# Workflow-Runner MCP

A locked-down MCP server that lets an external AI agent **run** MOD workflows —
and cancel, restart, and inspect runs — through your own LLM. It is a thin
adapter over the MOD mechanical workflow-run API (`/v1/workflow-runner/*`): it
holds no orchestration logic and no server-side LLM. Every call forwards your
API key, so the API's scope, read-only, and tenant checks apply unchanged.

## Auth

Mint a MOD API key carrying the **`workflow:run`** scope. A key without it (or a
read-only key, or a browser session) is rejected by the API.

## Run

```bash
cd CORE/workflow-runner-mcp
pip install -r requirements.txt
MOD_API_URL=https://api.your-mod-instance \
MOD_API_KEY=sk_... \
python -m workflow_runner_mcp.server
```

Then point an MCP-capable client (a coding agent) at the stdio server.

## Tools (mechanical only)

| Tool | Does |
|------|------|
| `run_workflow_version` | Run a workflow version over given input file ids |
| `cancel_run` | Cancel a running/queued run |
| `restart_run` | Restart a finished run (`full` or `failed_only`) |
| `get_run_status` | DB-backed run status |
| `get_run_progress` | Percent complete / current node / ETA |
| `get_run_node_status` | Per-node execution status |
| `get_run_cost` | Per-node cost breakdown |

There is deliberately **no** authoring / generate / describe tool. Running a
workflow requires the caller's tenant to have Harbor credentials configured,
exactly like the in-app run path.

## Per-session caller key

A stdio session acts ONLY with the caller's own credential. Supply it when the
session starts: `MOD_MCP_CALLER_KEY=<the caller's own MOD API key>` (preferred
name). `MOD_API_KEY` is still accepted as the single-caller back-compat name and
must also be the caller's own key — never a platform/service key. With no
caller credential the server refuses every tool call outright (no request is
sent). Keys owned by a service principal (tenant service account, or the
platform service user) are rejected by the API on every fleet router.
