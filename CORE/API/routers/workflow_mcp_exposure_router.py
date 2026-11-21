"""mcpt.a2/a3 (decomp-4.5 rows mcpt.a2-a6) — EXPOSE a workflow as an MCP tool.

    POST  /v1/road/{workflow_id}/mcp-exposures            {"tool_name": "..."}  -> 201 row
    GET   /v1/road/{workflow_id}/mcp-exposures                                     -> {"exposures": [...]}
    PATCH /v1/road/{workflow_id}/mcp-exposures/{exposure_id}  {"enabled": false} -> row

All business logic lives in ``services/workflow_mcp_exposure_service.py``; this
file only authenticates, authorizes, maps domain errors to HTTP and shapes the
bodies.

Auth is EXACTLY what routers/workflows_domains/workflows.py uses to edit a
workflow: the ``require_active_and_onboarded_user`` user dependency plus the
``_user_can_access_workflow(db, current_user, wf, "edit")`` capability check
(reused from routers/workflows_domains/_shared.py, not re-implemented) —
publishing steers what the tenant's compute runs, the same class of change as
editing the graph. A user without the 'edit' grant gets a 404 (the same
non-leak answer workflows.py gives for a failed capability check).

Tenant isolation: the service scopes every query to ``current_user.tenant_id``,
so another tenant's workflow or exposure is 404, never a leak. There is no
tenant override of any kind — the tenant comes only from the authenticated
caller.

main.py mount (the lead adds it; NOT edited here):
    from routers.workflow_mcp_exposure_router import router as workflow_mcp_exposure_router
    app.include_router(workflow_mcp_exposure_router, prefix="/v1")
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from routers.workflows_domains._shared import _user_can_access_workflow
from crud import workflow_cud as wf_crud
import services.workflow_mcp_exposure_service as exp_svc
from services import mcp_tools_service as mcp_svc
from services._base import ServiceError as ServiceErrorDomain

router = APIRouter(prefix="/road", tags=["The Road", "mcp-tools"])


def _raise_http(exc) -> None:
    """Map a service-layer ServiceError to HTTPException (same shape as the
    other domain routers: plain-string detail)."""
    status = {"forbidden": 403, "not_found": 404, "conflict": 409,
              "bad_request": 400}.get(exc.code, 400)
    raise HTTPException(status_code=status, detail=exc.message)


class McpExposureRow(BaseModel):
    """One ``WorkflowMcpExposure`` row in the response shape."""
    model_config = ConfigDict(extra="allow")
    id: int
    workflow_id: int | None = None
    tenant_id: int | None = None
    tool_name: str
    enabled: bool
    created_at: Any = None
    updated_at: Any = None


class McpExposureCreateRequest(BaseModel):
    """POST body: the MCP tool name to expose this workflow under."""
    model_config = ConfigDict(extra="forbid")
    tool_name: str


class McpExposureToggleRequest(BaseModel):
    """PATCH body: enable / disable an exposure (disable is SOFT)."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class McpExposureListResponse(BaseModel):
    """GET envelope: every exposure for the workflow, enabled included."""
    model_config = ConfigDict(extra="allow")
    exposures: List[McpExposureRow]


class McpPublishToggleRequest(BaseModel):
    """PATCH body: enable / disable MCP publishing for the workflow."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class McpPublishResponse(BaseModel):
    """Response envelope for the publish-toggle endpoint."""
    model_config = ConfigDict(extra="allow")
    workflow_id: int
    publish_mcp: bool


@router.post("/{workflow_id:int}/mcp-exposures",
             response_model=McpExposureRow, status_code=201)
def expose_workflow_mcp_tool(
    workflow_id: int,
    payload: McpExposureCreateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> McpExposureRow:
    """Expose one workflow as an MCP tool (creates the row + its trigger).

    201 with the exposure row. 400 bad ``tool_name`` / no version to pin the
    trigger to; 403 the tenant's MCP-publishing flag is off; 404 the workflow
    is missing, soft-deleted, in another tenant, or the caller lacks the
    'edit' capability (same non-leak answer workflows.py gives); 409 the
    ``tool_name`` is already used by another workflow of this tenant.
    Re-exposing the same (workflow, tool_name) is idempotent: the existing
    row is re-enabled and returned (still 201), and exactly one trigger
    exists.
    """
    db_workflow = wf_crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "edit"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        row = exp_svc.expose(db, tenant_id=current_user.tenant_id,
                             workflow_id=workflow_id,
                             tool_name=payload.tool_name,
                             actor_user_id=current_user.id)
    except ServiceErrorDomain as exc:
        _raise_http(exc)
    return McpExposureRow(id=row.id, workflow_id=row.workflow_id,
                          tenant_id=row.tenant_id, tool_name=row.tool_name,
                          enabled=bool(row.enabled), created_at=row.created_at,
                          updated_at=row.updated_at)


@router.get("/{workflow_id:int}/mcp-exposures",
            response_model=McpExposureListResponse)
def list_workflow_mcp_exposures(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> McpExposureListResponse:
    """List the workflow's MCP exposures (enabled and disabled alike).

    404 when the workflow is missing / soft-deleted / foreign, or the caller
    lacks the 'view' capability.
    """
    db_workflow = wf_crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "view"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        rows = exp_svc.list_for_workflow(db, tenant_id=current_user.tenant_id,
                                         workflow_id=workflow_id)
    except ServiceErrorDomain as exc:
        _raise_http(exc)
    return McpExposureListResponse(
        exposures=[McpExposureRow(id=r.id, workflow_id=r.workflow_id,
                                  tenant_id=r.tenant_id, tool_name=r.tool_name,
                                  enabled=bool(r.enabled),
                                  created_at=r.created_at, updated_at=r.updated_at)
                   for r in rows])


@router.patch("/{workflow_id:int}/mcp-exposures/{exposure_id:int}",
              response_model=McpExposureRow)
def toggle_workflow_mcp_exposure(
    workflow_id: int,
    exposure_id: int,
    payload: McpExposureToggleRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> McpExposureRow:
    """Enable / disable one exposure (disable is SOFT — the row always stays).

    404 when the workflow is missing / foreign / soft-deleted, the caller
    lacks the 'edit' capability, or the exposure belongs to another tenant
    OR another workflow (even in this tenant); 409/400 are not reachable
    here. Idempotent: re-setting the same value writes no audit row.
    """
    db_workflow = wf_crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "edit"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        row = exp_svc.set_enabled(db, tenant_id=current_user.tenant_id,
                                  workflow_id=workflow_id,
                                  exposure_id=exposure_id,
                                  enabled=payload.enabled,
                                  actor_user_id=current_user.id)
    except ServiceErrorDomain as exc:
        _raise_http(exc)
    return McpExposureRow(id=row.id, workflow_id=row.workflow_id,
                          tenant_id=row.tenant_id, tool_name=row.tool_name,
                          enabled=bool(row.enabled), created_at=row.created_at,
                          updated_at=row.updated_at)


@router.patch("/{workflow_id:int}/mcp-publish",
              response_model=McpPublishResponse)
def toggle_workflow_publish_mcp(
    workflow_id: int,
    payload: McpPublishToggleRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> McpPublishResponse:
    """Enable / disable MCP publishing for the workflow.

    404 when the workflow is missing / soft-deleted / foreign, or the caller
    lacks the 'edit' capability (same non-leak answer workflows.py gives);
    403 when ``enabled=true`` but the tenant's MCP-publishing flag is off.
    Writes an audit event on change.
    """
    db_workflow = wf_crud.get_workflow_by_id(db, workflow_id=workflow_id,
                                             tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "edit"):
        raise HTTPException(status_code=404, detail="Workflow not found")

    # 1900BR4 — platform-internal (is_system) workflows are bookkeeping rows the
    # scheduler drives; they must never be MCP-published. Without this the write
    # below silently mutated the seeded HarborScanFollowup sweep (and on a stack
    # whose workflow_mcp_exposure table/column was not yet applied it raised,
    # which is the 500 1599b's browser run saw). Same domain error the service
    # raises on the sibling endpoints, so the answer is identical (409).
    if bool(getattr(db_workflow, "is_system", False)):
        raise HTTPException(status_code=409,
                            detail="System workflows cannot be exposed or published as MCP tools")

    # Gate: enabling requires the tenant MCP-publishing flag to be on.
    if payload.enabled and not mcp_svc.publish_enabled(db, current_user.tenant_id):
        raise HTTPException(status_code=403,
                            detail="MCP publishing is not enabled for this tenant")

    # Persist the toggle + audit the action in one atomic commit (same
    # ordering as the sibling toggle_workflow_mcp_exposure/set_enabled: the
    # audit helper only ``db.add()``s — it never commits — so the commit
    # must come AFTER it or the audit row is silently rolled back when the
    # real ``get_db`` dependency closes the session at request teardown).
    db_workflow.publish_mcp = bool(payload.enabled)
    exp_svc._audit(db, "mcp.workflow_publish_toggled", current_user.tenant_id,
                   current_user.id,
                   {"workflow_id": workflow_id, "publish_mcp": bool(payload.enabled)})
    db.commit()

    return McpPublishResponse(workflow_id=workflow_id,
                              publish_mcp=bool(db_workflow.publish_mcp))
