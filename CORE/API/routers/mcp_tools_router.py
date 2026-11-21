"""sz-mcp-per-workflow.3 (mcp-fleet §per-workflow) — list exposed workflows as
MCP tools.

GET /v1/mcp-tools/tools/list returns every WorkflowMcpExposure row where
``enabled IS TRUE`` and the referenced Workflow is not soft-deleted, folded
into the MCP ``tools/list`` response shape (name, description, inputSchema,
outputSchema, workflow_id).

The tenant scope comes ONLY from the authenticated caller
(``current_user.tenant_id``) — there is no query param, header, or internal
override of any kind, so an authenticated caller can never read another
tenant's exposures.  All DB work lives in
``services/mcp_tools_service.py:list_exposed_tools``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

import services.mcp_tools_service as mcp_svc
import services.triggers.mcp_tool_trigger as mcp_call_svc
from dependencies import get_db, require_workflow_run_key
import models  # noqa: E402
from services import platform_settings  # noqa: E402

router = APIRouter(prefix="/mcp-tools", tags=["mcp-tools"])

# ---------------------------------------------------------------------------
# Module-level setting registration (sz-mcp-per-workflow.4)
# ---------------------------------------------------------------------------
platform_settings.register(
    "mcp_tools.stream_max_seconds",
    int,
    300,
    group="mcp_tools",
    validator=platform_settings._validate_non_negative_int,
    description="sz-mcp-per-workflow.4 — maximum seconds the SSE stream at "
                "POST /mcp-tools/tools/call/stream polls a WorkflowRun for its "
                "status. Default 300.",
)


class McpToolRow(BaseModel):
    """One MCP tool row in the tools/list shape."""
    model_config = ConfigDict(extra="allow")
    name: str
    description: str
    inputSchema: Dict[str, Any]
    outputSchema: Dict[str, Any]
    workflow_id: int


class McpToolsListResponse(BaseModel):
    """MCP ``tools/list`` envelope: ``{"tools": [...]}``."""
    model_config = ConfigDict(extra="allow")
    tools: List[McpToolRow]


@router.get("/tools/list", response_model=McpToolsListResponse)
def list_mcp_tools(
    db: Session = Depends(get_db),
    current_user=Depends(require_workflow_run_key),
) -> McpToolsListResponse:
    """List enabled MCP exposures as MCP tools (tools/list).

    Returns ``{"tools": [...]}`` sorted by ``name``, capped at 500, scoped
    strictly to the authenticated caller's tenant.
    """
    if not mcp_svc.publish_enabled(db, current_user.tenant_id):
        return McpToolsListResponse(tools=[])
    tools = mcp_svc.list_exposed_tools(db, current_user.tenant_id)
    return McpToolsListResponse(tools=tools)


# --- mcpt.a3: POST /tools/call ---------------------------------------------


class McpToolCallRequest(BaseModel):
    """MCP ``tools/call`` body: tool name + arguments + caller idempotency key."""
    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: Dict[str, Any]
    idempotency_key: str


class McpToolCallResponse(BaseModel):
    """``tools/call`` envelope: what the pipeline decided."""
    model_config = ConfigDict(extra="allow")
    dispatched: bool
    run_id: int | None
    reason: str | None


@router.post("/tools/call", response_model=McpToolCallResponse,
             response_model_exclude_unset=True)
def call_mcp_tool(payload: McpToolCallRequest,
                  db: Session = Depends(get_db),
                  current_user=Depends(require_workflow_run_key)) -> McpToolCallResponse:
    """Fire one exposed-workflow MCP tool call (tools/call).

    200 ``{"dispatched", "run_id", "reason"}`` — the pipeline's own verdict
    (dedup / rate-limit / conditions can hold a fire back; that is still a
    well-formed 200).  400 ``{"detail": {"error": "schema_mismatch", "property":
    <json path>}}`` when the arguments violate the exported input schema.  404
    (generic, identical for unknown / disabled / foreign-tenant tools and a
    workflow with no live trigger) — the anti-enumeration trap.
    """
    if not mcp_svc.publish_enabled(db, current_user.tenant_id):
        raise HTTPException(status_code=404,
                            detail="MCP tool not found for this tenant")
    try:
        result = mcp_call_svc.call_exposed_tool(
            db,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            name=payload.name,
            arguments=payload.arguments,
            idempotency_key=payload.idempotency_key,
        )
    except LookupError:
        raise HTTPException(status_code=404,
                            detail="MCP tool not found for this tenant")
    except ValueError as bad:
        prop = str(bad.args[0]) if bad.args else "$"
        return JSONResponse(status_code=400,
                            content={"detail": {"error": "schema_mismatch",
                                                "property": prop}})
    return McpToolCallResponse(dispatched=result["dispatched"],
                               run_id=result["run_id"],
                               reason=result["reason"])


# --- mcpt.a5: POST /tools/call/stream --------------------------------------


class _McpToolCallRequest(BaseModel):
    """MCP ``tools/call`` body: tool name + arguments + caller idempotency key."""
    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: Dict[str, Any]
    idempotency_key: str


@router.post("/tools/call/stream", response_model=None)
async def call_mcp_tool_stream(
    payload: _McpToolCallRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_workflow_run_key),
) -> StreamingResponse:
    """Fire one exposed-workflow MCP tool call and stream its progress.

    SSE event shapes::

        data: {"event": "started", "run_id": 42, "tool": "alpha-tool"}

        data: {"event": "status", "run_id": 42, "status": "running",
               "node_id": "n3", "node_status": "running"}

        data: {"event": "done", "run_id": 42, "status": "success"}
        data: {"event": "error", "run_id": 42, "detail": "timeout"}

    The stream is a short-poll loop over the WorkflowRun / NodeRun rows.
    It is bounded by the ``mcp_tools.stream_max_seconds`` platform setting
    (default 300 s).  The non-streaming ``POST /tools/call`` endpoint is
    unaffected.
    """
    # Re-use the same guard as the sync endpoint so rate-limit / publish
    # logic is identical between the two paths.
    if not mcp_svc.publish_enabled(db, current_user.tenant_id):
        raise HTTPException(status_code=404,
                            detail="MCP tool not found for this tenant")
    try:
        result = mcp_call_svc.call_exposed_tool(
            db,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            name=payload.name,
            arguments=payload.arguments,
            idempotency_key=payload.idempotency_key,
        )
    except LookupError:
        raise HTTPException(status_code=404,
                            detail="MCP tool not found for this tenant")
    except ValueError as bad:
        # Mirror the sync endpoint's schema_mismatch contract exactly; this
        # happens before any bytes are sent, so a normal JSON 400 is still
        # possible here (unlike errors raised once streaming has started).
        prop = str(bad.args[0]) if bad.args else "$"
        return JSONResponse(status_code=400,
                            content={"detail": {"error": "schema_mismatch",
                                                "property": prop}})

    run_id = result.get("run_id")
    if run_id is None:
        # Dispatch was refused (dedup / rate-limit).  Emit a single error
        # event and close the stream immediately.
        async def _early_close() -> AsyncGenerator[str, None]:
            ev = {"event": "error", "run_id": None,
                  "detail": result.get("reason", "rejected")}
            yield f"data: {json.dumps(ev)}\n\n"
        return StreamingResponse(_early_close(), media_type="text/event-stream")

    poll_interval = 2.0  # seconds between polls
    # Read the max-duration from the platform-settings registry (no env var).
    try:
        max_seconds = platform_settings.get_setting("mcp_tools.stream_max_seconds", db)
    except Exception:  # setting unregistered or DB error — fall back to default
        max_seconds = 300

    terminal_statuses = frozenset({
        models.TaskStatus.SUCCESS.value,
        models.TaskStatus.COMPLETED.value,
        models.TaskStatus.FAILED.value,
        models.TaskStatus.FAILURE.value,
    })

    async def _stream() -> AsyncGenerator[str, None]:
        elapsed = 0.0
        last_run_status: str | None = None
        last_node_status: str | None = None
        last_node_id: str | None = None

        yield f'data: {json.dumps({"event": "started", "run_id": run_id, "tool": payload.name})}\n\n'

        while elapsed < max_seconds:
            # populate_existing=True forces a fresh SELECT instead of
            # returning the cached identity-map row: without it, every
            # poll after the first would just re-read the same in-memory
            # object and the stream would never observe a status change
            # made by another transaction (the orchestrator / worker).
            run = db.get(models.WorkflowRun, run_id, populate_existing=True)

            if run is None:
                break

            run_status = run.status.value if run.status else None

            if run_status != last_run_status:
                ev = {"event": "status", "run_id": run_id, "status": run_status}
                yield f"data: {json.dumps(ev)}\n\n"
                last_run_status = run_status

            # Also surface the latest NodeRun status if available.
            node_status, node_id = _latest_node_status(db, run_id)
            if node_status != last_node_status or node_id != last_node_id:
                ev = {"event": "status", "run_id": run_id,
                      "node_id": node_id, "node_status": node_status}
                yield f"data: {json.dumps(ev)}\n\n"
                last_node_status = node_status
                last_node_id = node_id

            if run_status in terminal_statuses:
                ev = {"event": "done", "run_id": run_id, "status": run_status}
                yield f"data: {json.dumps(ev)}\n\n"
                break

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        else:
            # max_seconds elapsed without terminal state.
            ev = {"event": "error", "run_id": run_id, "detail": "stream_timeout"}
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


def _latest_node_status(session: Session, run_id: int):
    """Return (status_value, node_id) for the most recently completed NodeRun."""
    from sqlalchemy import select, desc  # noqa: E402
    stmt = (
        select(models.NodeRun.status, models.NodeRun.id)
        .where(models.NodeRun.workflow_run_id == run_id)
        .order_by(desc(models.NodeRun.id))
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        return None, None
    return row[0].value if row[0] else None, row[1]
