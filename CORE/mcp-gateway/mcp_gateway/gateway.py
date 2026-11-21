"""MOD MCP fleet gateway.

Hosts all six fleet MCP servers over a single Streamable-HTTP ASGI app, each
mounted at ``/mcp/<tool>``. Every server is built with **no baked-in key**: each
MCP connection forwards its own MOD API key (``X-API-Key`` or
``Authorization: Bearer``), which the per-tool client resolves per request and
passes to the mechanical ``/v1/<tool>/*`` API. The gateway therefore holds no
ambient authority — it grants nothing the caller's key does not already have, and
a key scoped to one tool is still rejected by every other tool's API router.

**Login required (owner decision 11-14):** a connection must present a MOD
credential (`X-API-Key` or `Authorization: Bearer`) BEFORE the gateway serves
anything under `/mcp/*` — the `tools/list` handshake included. An anonymous
request gets 401 `{"error": "authentication required"}` and no tool name is
revealed; `/healthz` stays status-only and open.

Config (environment):
  MOD_API_URL   base URL the tool clients call (e.g. http://my_fastapi_app in the
                stack, or https://api.<domain>). Required.
  MCP_HOST/MCP_PORT  bind address (default 0.0.0.0:8000).

Run:
  MOD_API_URL=http://my_fastapi_app uvicorn mcp_gateway.gateway:app --host 0.0.0.0 --port 8000

Client connection URL per tool:  <gateway-url>/mcp/<tool>/mcp
"""
from __future__ import annotations

import os
from contextlib import AsyncExitStack, asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

# Login gate (owner decision 11-14 8:35 PM CT): /mcp/* requires a login BEFORE
# anything is served — including the MCP tools/list handshake. Importable
# without the `mcp` package (starlette + httpx only). See mcp_gateway/auth.py.
from mcp_gateway.auth import build_login_gate

# Each fleet server exposes build_http_app() -> Streamable-HTTP ASGI app with no
# baked key. Imported here so the gateway mounts one process for the whole fleet.
from node_wizard_mcp.server import build_http_app as node_wizard_app
from workflow_runner_mcp.server import build_http_app as workflow_runner_app
from automations_mcp.server import build_http_app as automations_app
from workflow_wizard_mcp.server import build_http_app as workflow_wizard_app
from project_wizard_mcp.server import build_http_app as project_wizard_app
from stack_wizard_mcp.server import build_http_app as stack_wizard_app

# Mount path -> server factory. Path segment is the tool's public name.
TOOL_APPS = {
    "node-wizard": node_wizard_app,
    "workflow-runner": workflow_runner_app,
    "automations": automations_app,
    "workflow-wizard": workflow_wizard_app,
    "project-wizard": project_wizard_app,
    "stack": stack_wizard_app,
}


def build_gateway() -> Starlette:
    """Build the combined ASGI app. Each sub-app's lifespan (which starts its
    Streamable-HTTP session manager) is entered by the parent lifespan — mounting
    alone does not run a sub-app's lifespan."""
    subapps = {name: factory() for name, factory in TOOL_APPS.items()}

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with AsyncExitStack() as stack:
            for sub in subapps.values():
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield

    async def healthz(_req: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    routes = [Route("/healthz", healthz)]
    routes += [Mount(f"/mcp/{name}", app=sub) for name, sub in subapps.items()]
    inner = Starlette(routes=routes, lifespan=lifespan)
    # Login gate: /mcp/* (tools/list included) needs a valid MOD credential;
    # /healthz stays status-only and open. Fail-closed when MOD_API_URL is unset.
    return build_login_gate(inner, api_base_url=os.getenv("MOD_API_URL", ""))


app = build_gateway()


def main() -> None:
    import uvicorn

    host = os.getenv("MCP_HOST", "0.0.0.0").strip()
    port = int(os.getenv("MCP_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
