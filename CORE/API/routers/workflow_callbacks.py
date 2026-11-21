# API/routers/workflow_callbacks.py

"""
Workflow Callbacks

Endpoints for the workflow orchestrator (services.workflow_orchestrator)
to call when workflows complete/fail. Used for cleanup and usage
tracking (e.g., decrement concurrent processes).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
import models
from dependencies import get_db, require_bearer_or_api_key
from utils.usage_enforcement import decrement_concurrent_processes
from services import workflow_callbacks_service
from services.workflow_callbacks_service import ForbiddenError
from services._base import NotFoundError
import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["The Road", "Workflow Callbacks"],
    prefix="/road/callbacks"
)


class WorkflowCompletionCallback(BaseModel):
    """Callback payload when a workflow completes (from the orchestrator)."""
    workflow_run_id: int
    status: str  # "success", "failed", "canceled"
    tenant_id: int


@router.post("/complete", status_code=status.HTTP_200_OK)
def workflow_completion_callback(
    callback_data: WorkflowCompletionCallback,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key)  # Service API key required
):
    """
    Called when a workflow run completes, fails, or is canceled.

    This endpoint:
    1. Updates workflow run status
    2. Decrements concurrent process counter
    3. Could add final credit reconciliation (future)

    Requires API key authentication (not user auth).
    """
    # S11: Derive tenant_id from authenticated user, not request body (IDOR fix)
    tenant_id = current_user.tenant_id

    try:
        result = workflow_callbacks_service.process_completion(
            db,
            workflow_run_id=callback_data.workflow_run_id,
            status_str=callback_data.status,
            tenant_id=tenant_id,
        )
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow run not found"
        )
    except ForbiddenError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )

    # Decrement concurrent process counter only if we actually transitioned to
    # terminal state. Kept here (not in the service) so existing tests that
    # patch `routers.workflow_callbacks.decrement_concurrent_processes` keep
    # working — see services/workflow_callbacks_service.py's module docstring.
    if result.should_decrement:
        decrement_concurrent_processes(db=db, tenant_id=callback_data.tenant_id)
        logger.info(f"📉 Decremented concurrent processes for tenant {callback_data.tenant_id}")

    return {
        "status": "success",
        "message": f"Workflow run {callback_data.workflow_run_id} completion processed",
        "workflow_run_id": callback_data.workflow_run_id
    }
