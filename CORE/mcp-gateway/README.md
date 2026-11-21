# MOD MCP fleet gateway

Hosts all six fleet MCP servers over a single **Streamable-HTTP** ASGI app, each
mounted at `/mcp/<tool>`. It is the network-reachable, stack-hosted counterpart to
the stdio fleet servers (which a client spawns locally).

## Login required (11-14 owner decision)
The gateway **requires a login** before it serves anything under `/mcp/*` — the
MCP `tools/list` handshake included. A connection must send its MOD credential
(`X-API-Key` or `Authorization: Bearer`); an anonymous request gets 401
`{"error": "authentication required"}` with no tool names, and an unreachable
`MOD_API_URL` fails closed (503). `/healthz` stays status-only and open.
The gateway holds **no** baked-in MOD API key. Each MCP connection forwards its
own key (`X-API-Key` or `Authorization: Bearer`), which the per-tool client
resolves per request (from the MCP request context) and passes to the mechanical
`/v1/<tool>/*` API. So the gateway grants nothing the caller's key does not
already have, and a key scoped to one tool is still rejected by every other
tool's API router.

## Auth-cache revocation window
A positive login verdict is cached in memory for **60 s** per credential
(`CACHE_TTL_SECONDS = 60.0`), so a credential that has just been **revoked or
deactivated can still list tools for up to 60 s after revocation** — the gate
re-probes only once the cached verdict expires. The cache holds at most **256
entries** (`CACHE_MAX_ENTRIES = 256`): expired entries are dropped first, then
the oldest entry is evicted. An unreachable `MOD_API_URL` is never cached — it
fails closed (503).

## Tools / mount paths
| Mount | Tool | Scope |
|---|---|---|
| `/mcp/node-wizard` | node-wizard | `node:author` |
| `/mcp/workflow-runner` | workflow-runner | `workflow:run` |
| `/mcp/automations` | automations | `automation:manage` |
| `/mcp/workflow-wizard` | workflow-wizard | `workflow:author` |
| `/mcp/project-wizard` | project-wizard | `project:author` |
| `/mcp/stack` | stack | `stack:author` (+ platform capability) |

Client connect URL per tool: `<gateway-url>/mcp/<tool>/mcp`.

## Run
```
# In the stack (compose service my_mcp_gateway):
MOD_API_URL=http://my_fastapi_app uvicorn mcp_gateway.gateway:app --host 0.0.0.0 --port 8000

# Build the image (context MUST be CORE/ — it COPYs sibling *-mcp packages):
docker build -f mcp-gateway/Dockerfile -t mod-mcp-gateway .
```

Config: `MOD_API_URL` (required), `MCP_HOST`/`MCP_PORT` (default `0.0.0.0:8000`).
Health: `GET /healthz`. /healthz returns status only; tool names are not listed on the unauthenticated endpoint..
