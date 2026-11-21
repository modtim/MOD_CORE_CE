# Project-Wizard MCP (mechanical core)

A locked-down MCP server exposing the **mechanical** core of the MOD Project
Wizard. The project wizard is an LLM-driven agentic feature; its only non-LLM
seam is recording the grading example (criteria), which is all this server
exposes. The conversational turn and the tick/wake pumping are NOT exposed —
the client agent brings its own LLM.

## Auth
Mint a MOD API key with the **`project:author`** scope.

## Run
```bash
cd CORE/project-wizard-mcp
pip install -r requirements.txt
MOD_API_URL=https://api.your-mod-instance MOD_API_KEY=sk_... python -m project_wizard_mcp.server
```

## Tools (mechanical only)
| Tool | Does |
|------|------|
| `record_criteria` | Record the grading example a project session is built against |

No `turn` / `tick` / `wake` / `generate` tool exists on this surface.

## Per-session caller key

A stdio session acts ONLY with the caller's own credential. Supply it when the
session starts: `MOD_MCP_CALLER_KEY=<the caller's own MOD API key>` (preferred
name). `MOD_API_KEY` is still accepted as the single-caller back-compat name and
must also be the caller's own key, never a service key. With no caller
credential the server refuses every tool call outright (no request is sent).
Keys owned by a service account are rejected by the API.
