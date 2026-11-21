# Stack MCP

A locked-down MCP server for managing MOD **persistent stacks** — list, create,
get, start, stop — through your own LLM. Thin adapter over `/v1/stack/*`; no
server-side LLM (the stack wizard's `from-wizard` codegen path is not exposed).

## Auth
Mint a MOD API key with the **`stack:author`** scope; its owner must also hold
the `stack:author` platform capability (parity with the in-app stack router).

## Run
```bash
cd CORE/stack-wizard-mcp
pip install -r requirements.txt
MOD_API_URL=https://api.your-mod-instance MOD_API_KEY=sk_... python -m stack_wizard_mcp.server
```

## Tools (mechanical only)
| Tool | Does |
|------|------|
| `list_stacks` | List the caller tenant's stacks |
| `create_stack` | Create a stack from a compose spec |
| `get_stack` | Get one stack |
| `start_stack` | Start (deploy) a stack |
| `stop_stack` | Stop a stack without teardown |

No `from-wizard` / codegen tool exists on this surface.

## Per-session caller key

A stdio session acts ONLY with the caller's own credential. Supply it when the
session starts: `MOD_MCP_CALLER_KEY=<the caller's own MOD API key>` (preferred
name). `MOD_API_KEY` is still accepted as the single-caller back-compat name and
must also be the caller's own key — never a platform/service key. With no
caller credential the server refuses every tool call outright (no request is
sent). Keys owned by a service principal (tenant service account, or the
platform service user) are rejected by the API on every fleet router.
