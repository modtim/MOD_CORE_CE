# Automations MCP

A locked-down MCP server for **wiring workflows to events and watching them
work** — the full trigger lifecycle plus read-only observability — driven by
your own LLM. Thin adapter over `/v1/automations/*`; no server-side LLM. Every
call forwards your API key and is tenant-scoped.

## Auth
Mint a MOD API key with the **`automation:manage`** scope. This is a write scope
(creating triggers is powerful — a webhook trigger mints a public firing URL),
so read-only keys are rejected.

## Run
```bash
cd CORE/automations-mcp
pip install -r requirements.txt
MOD_API_URL=https://api.your-mod-instance MOD_API_KEY=sk_... python -m automations_mcp.server
```

## Tools
| Tool | Does |
|------|------|
| `create_trigger` | Create a trigger of any type for a workflow you own |
| `list_triggers` / `get_trigger` | List / read your triggers |
| `update_trigger` / `delete_trigger` | Edit / soft-delete a trigger |
| `rotate_trigger_secret` | Rotate a webhook/form secret (returned once) |
| `get_trigger_events` | Recent event-arrival history for a trigger |
| `get_run_log` | Detailed per-node execution log for a run |

The raw platform API-log stream (what `log_pattern_alert` triggers match) is
**not** exposed here.

## Per-session caller key

A stdio session acts ONLY with the caller's own credential. Supply it when the
session starts: `MOD_MCP_CALLER_KEY=<the caller's own MOD API key>` (preferred
name). `MOD_API_KEY` is still accepted as the single-caller back-compat name and
must also be the caller's own key — never a platform/service key. With no
caller credential the server refuses every tool call outright (no request is
sent). Keys owned by a service principal (tenant service account, or the
platform service user) are rejected by the API on every fleet router.
