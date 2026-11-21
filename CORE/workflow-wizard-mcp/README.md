# Workflow-Wizard MCP (mechanical core)

A locked-down MCP server exposing the **mechanical** core of the MOD Workflow
Wizard to an external agent that brings its own LLM. Thin adapter over
`/v1/workflow-authoring/*`; the server-side LLM generate/turn is **not**
exposed. Every call forwards your API key.

## Auth
Mint a MOD API key with the **`workflow:author`** scope.

## Run
```bash
cd CORE/workflow-wizard-mcp
pip install -r requirements.txt
MOD_API_URL=https://api.your-mod-instance MOD_API_KEY=sk_... python -m workflow_wizard_mcp.server
```

## Tools (mechanical only)
| Tool | Does |
|------|------|
| `apply_proposal` | Materialize a session's stored proposal into a workflow version |
| `test_run` | Sandbox test-run of a session proposal |
| `get_test_run_status` | Status of a sandbox test-run |
| `queue_missing_nodes` | Queue container builds for a proposal's missing nodes |
| `record_audit_action` | Record an action on a wizard audit finding |

No `generate` / `turn` / `describe` tool exists — the LLM reasoning is yours.

## Per-session caller key

A stdio session acts ONLY with the caller's own credential. Supply it when the
session starts: `MOD_MCP_CALLER_KEY=<the caller's own MOD API key>` (preferred
name). `MOD_API_KEY` is still accepted as the single-caller back-compat name and
must also be the caller's own key — never a platform/service key. With no
caller credential the server refuses every tool call outright (no request is
sent). Keys owned by a service principal (tenant service account, or the
platform service user) are rejected by the API on every fleet router.
