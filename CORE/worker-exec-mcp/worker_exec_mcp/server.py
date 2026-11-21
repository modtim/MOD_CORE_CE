"""MCP server exposing the mechanical host-exec tools.

Config (environment):
  MOD_API_URL    base URL of the MOD instance (e.g. https://api.your-mod-instance)
  MOD_API_KEY    an API key carrying worker-exec scope (X-API-Key).
                 Required for stdio; omitted for hosted (streamable-http) mode,
                 where each connection forwards its own key.
  MCP_TRANSPORT  "stdio" (default) or "http" (Streamable-HTTP, for hosting).
  MCP_HOST/MCP_PORT  bind address for standalone http mode (default 0.0.0.0:8000).

Run (stdio, local):
  MOD_API_URL=... MOD_API_KEY=sk_... python -m worker_exec_mcp.server

Run (hosted, network-reachable):
  MOD_API_URL=... MCP_TRANSPORT=http python -m worker_exec_mcp.server

The server holds no ambient authority: every tool call forwards the caller's key
to the API, so the API-key scope, read-only rejection, and tenant/user scoping
all apply unchanged. In hosted mode no key is baked in — the per-connection key
is forwarded instead. The `mcp` dependency is imported lazily so the package
(client + tools) stays importable for tests without the MCP runtime installed.
"""
from __future__ import annotations

import os
import sys

from .client import WorkerExecClient
from .tools import build_tools

SERVER_NAME = "worker-exec"


def _base_url() -> str:
    base_url = os.getenv("MOD_API_URL", "").strip()
    if not base_url:
        print("MOD_API_URL is required (base URL of your MOD instance).", file=sys.stderr)
        sys.exit(2)
    return base_url


def build_mcp(*, require_key: bool):
    """Construct the FastMCP server with the host-exec tools registered.

    require_key=True (stdio): a baked-in MOD_API_KEY is required.
    require_key=False (hosted http): no baked-in key — each connection forwards
    its own credential, resolved per-call by the client.
    """
    from mcp.server.fastmcp import FastMCP  # lazy: only needed to actually run

    base_url = _base_url()
    # A stdio session acts ONLY with the caller's own credential. Supply it when
    # the session starts: MOD_MCP_CALLER_KEY=<the caller's own MOD API key>
    # (preferred name). MOD_API_KEY is still accepted as the single-caller
    # back-compat name. With no caller credential the server refuses every tool
    # call outright (no request is sent). Keys owned by a service principal
    # (tenant service account, or the platform service user) are rejected by
    # the API on every fleet router.
    caller_key = os.getenv("MOD_MCP_CALLER_KEY", "").strip()
    api_key = caller_key or os.getenv("MOD_API_KEY", "").strip()
    if require_key and not api_key:
        print("A caller API key is required for stdio: set MOD_MCP_CALLER_KEY to "
              "the caller's own MOD API key (MOD_API_KEY is accepted as the "
              "single-caller back-compat name). No key = no tool call.",
              file=sys.stderr)
        sys.exit(2)

    client = WorkerExecClient(base_url, api_key if require_key else "")
    if require_key:
        client.set_session_caller_key(api_key)
    tools = build_tools(client)

    mcp = FastMCP(SERVER_NAME)
    for name, fn in tools.items():
        mcp.tool(name=name)(fn)
    return mcp


def build_http_app():
    """Return the Streamable-HTTP ASGI app for hosting/mounting (no baked key)."""
    return build_mcp(require_key=False).streamable_http_app()


def build_server():
    """Back-compat: the stdio server with a baked-in key."""
    return build_mcp(require_key=True)


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "http":
        import uvicorn

        host = os.getenv("MCP_HOST", "0.0.0.0").strip()
        port = int(os.getenv("MCP_PORT", "8000"))
        uvicorn.run(build_http_app(), host=host, port=port)
    else:
        build_mcp(require_key=True).run()  # stdio transport


if __name__ == "__main__":
    main()
