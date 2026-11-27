"""workflow-expressiveness-primitives 3.4 (slice 2) — POST by-interface run router.

Resolves an interface name → workflow via the resolver, picks the
workflow's latest version, and delegates to the existing run endpoint.

Mounted under /workflows (matching the goal_planner router pattern).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import crud, models, schemas
from dependencies import get_db, require_harbor_credentials, get_keycloak_token
from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from services import workflow_interface_resolver as resolver

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/road", tags=["The Road", "Workflow Interfaces"])


class ByInterfaceRunIn(BaseModel):
    implementation_id: Optional[int] = Field(
        default=None,
        description="Explicit implementation workflow id; when omitted the "
                    "tenant's default (from the expr interface-defaults setting) "
                    "is used, falling back to count-resolution.",
    )
    run: schemas.WorkflowRunRequest


@router.post(
    "/by-interface/{interface_name}/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=dict,
)
def run_by_interface(
    interface_name: str,
    body: ByInterfaceRunIn,
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_harbor_credentials),
    keycloak_token: str = Depends(get_keycloak_token),
) -> Dict[str, Any]:
    """Resolve a named interface to a workflow version and trigger a run."""
    tenant_id = current_user.tenant_id

    result = resolver.resolve_implementation(
        db,
        tenant_id=tenant_id,
        interface_name=interface_name,
        implementation_id=body.implementation_id,
    )

    status_code = result["status"]

    if status_code == "invalid_interface":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_interface", "message": "Interface name is not a valid slug"},
        )

    if status_code == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "Explicit implementation_id not found"},
        )

    if status_code == "no_implementation":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_implementation", "message": "No implementation found for this interface"},
        )

    if status_code == "ambiguous":
        candidates = result.get("candidates", [])
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "ambiguous", "candidates": candidates},
        )

    if status_code == "unavailable":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "unavailable", "message": "Interface resolution is unavailable"},
        )

    # status == "resolved" — proceed to delegate
    workflow_id = result.get("workflow_id")
    source = result.get("source", "")

    # Find the latest version of the resolved workflow
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=tenant_id)
    if db_workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_version", "message": "Resolved workflow not found for this tenant"},
        )

    version = db_workflow.latest_version
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_version", "message": "Workflow has no versions"},
        )

    # Delegate to the existing run endpoint.
    # GC.MWL.7c parity: the user's keycloak_token is deliberately NOT threaded
    # into trigger_workflow_run (it has no such parameter); the route keeps the
    # Depends(get_keycloak_token) so its bearer-token auth posture is unchanged.
    resp = _trigger_workflow_run(
        version_id=version.id,
        run_request=body.run,
        background_tasks=background_tasks,
        db=db,
        current_user=current_user,
    )

    # Wrap: add resolution metadata to the response
    if isinstance(resp, dict):
        resp["resolved_workflow_id"] = workflow_id
        resp["resolution_source"] = source
    else:
        # Shouldn't happen — _trigger_workflow_run returns a dict
        logger.error("unexpected return type from _trigger_workflow_run: %s", type(resp))
        resp = {"error": "internal_error", "resolved_workflow_id": workflow_id,
                "resolution_source": source}

    return resp


def _trigger_workflow_run(
    version_id: int,
    run_request: schemas.WorkflowRunRequest,
    background_tasks: BackgroundTasks,
    db: Session,
    current_user: models.User,
) -> Dict[str, Any]:
    """Call into the existing trigger_workflow_run logic.

    This is imported from workflows_domains.workflows at runtime so we
    don't hit circular import issues at module load time. GC.MWL.7c parity:
    no keycloak_token parameter — the user's token is deliberately NOT threaded.
    """
    from routers.workflows_domains.workflows import trigger_workflow_run as _twr
    return _twr(
        version_id=version_id,
        run_request=run_request,
        background_tasks=background_tasks,
        db=db,
        current_user=current_user,
    )
