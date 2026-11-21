# Node-Wizard MCP

An [MCP](https://modelcontextprotocol.io) server that lets an AI coding agent
build and register MOD nodes through the **external node-authoring API**
(`/v1/node-authoring/*`). It ships with the free/open MOD build and is the
open-core replacement for the optional Node Wizard: MOD provides the *tools*, your
agent brings the *intelligence* (it writes the Dockerfile on its own model and
token budget).

It is a thin adapter — it holds no node-building logic and talks only to the
public HTTP API, carrying your API key through unchanged. It can do nothing your
key cannot already do.

## Tools (mechanical only)

| Tool | What it does |
|------|--------------|
| `get_node_contract` | Fetch the versioned node contract (I/O, env config, allowlisted base, non-root, manifest fields, examples). |
| `list_base_images` | List the base-image allowlist you must build against. |
| `validate_submission` | Dry-run admission on a candidate submission — `{passed, rule, reason}`, creates nothing. |
| `register_node` | Register a pre-built, pullable image as a node (→ node id + scan status). |
| `build_node` | Build a self-contained Dockerfile then register it (→ a build task id to poll). |
| `get_build_status` | Poll a build task id. |

There is **no** code-generation / "describe" / containerize tool. The agent
authors the Dockerfile and iterates using `validate_submission`.

## Setup

```bash
pip install -r requirements.txt

export MOD_API_URL=https://api.your-mod-instance
export MOD_API_KEY=sk_...   # an API key carrying the 'node:author' scope
python -m node_wizard_mcp.server
```

Mint the key with `POST /api-keys/` and `capability_scope: ["node:author"]`
(a read-only key is rejected). The server communicates over stdio, so register
it with your MCP client as a stdio server, e.g.:

```json
{
  "mcpServers": {
    "node-wizard": {
      "command": "python",
      "args": ["-m", "node_wizard_mcp.server"],
      "env": { "MOD_API_URL": "https://api.your-mod-instance", "MOD_API_KEY": "sk_..." }
    }
  }
}
```

## Guarantees

- **No ambient authority** — every call forwards `MOD_API_KEY`; scope, read-only
  rejection, and tenant/user scoping are enforced by the API.
- **Not a stronger path than the API** — a submission the API would reject is
  rejected identically here; the MCP registers nothing on its own.
- **Private drafts only** — nodes you author land as private drafts, never
  system nodes and never auto-published.

## Per-session caller key

A stdio session acts ONLY with the caller's own credential. Supply it when the
session starts: `MOD_MCP_CALLER_KEY=<the caller's own MOD API key>` (preferred
name). `MOD_API_KEY` is still accepted as the single-caller back-compat name and
must also be the caller's own key — never a platform/service key. With no
caller credential the server refuses every tool call outright (no request is
sent). Keys owned by a service principal (tenant service account, or the
platform service user) are rejected by the API on every fleet router.
