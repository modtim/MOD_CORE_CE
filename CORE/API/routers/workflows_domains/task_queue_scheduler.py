# Auto-generated domain sub-module (Gate 2a split of routers/workflows.py).
# Behavior-preserving: endpoints MOVED verbatim, grouped by audit domain.
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, status, HTTPException, BackgroundTasks, Body, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import cast, String
import os
import schemas, crud, models
from dependencies import get_db, require_active_and_onboarded_user, get_keycloak_token, require_harbor_credentials, require_workflow_run_key
try:
    from utils.pricing import get_postpaid_credit_price, get_overage_credit_price
except ImportError:
    def get_postpaid_credit_price(*args, **kwargs):
        return 0

    def get_overage_credit_price(*args, **kwargs):
        return 0
from product_names import themed_logger
logger = themed_logger('routers.workflows_domains.task_queue_scheduler', 'workflows')
from pydantic import BaseModel
import json
import io
import re
import zipfile
from dependencies import require_bearer_or_api_key
from utils.run_task_auth import node_or_onboarded_user_principal, pin_run_task_tenant

from routers.workflows_domains._shared import *

import routers.workflows as _wf  # aggregator ns for monkeypatch-faithful delegation

workflow_runner_router = APIRouter()


@workflow_runner_router.post("/versions/{version_id}/run", status_code=status.HTTP_202_ACCEPTED)
def wr_run_version(
        version_id: int,
        run_request: schemas.WorkflowRunRequest,
        background_tasks: BackgroundTasks = None,
        db: Session = Depends(get_db),
        user: models.User = Depends(require_workflow_run_key),
):
    """Run a workflow version. Requires the caller's tenant to have Harbor
    credentials configured, exactly like the in-app run path."""
    harbor_user = _wf.require_harbor_credentials(db=db, current_user=user)
    return _wf.trigger_workflow_run(
        version_id=version_id,
        run_request=run_request,
        background_tasks=background_tasks,
        db=db,
        current_user=harbor_user,
        keycloak_token="",
    )

@workflow_runner_router.post("/runs/{run_id}/cancel", status_code=status.HTTP_200_OK)
def wr_cancel_run(
        run_id: int,
        db: Session = Depends(get_db),
        user: models.User = Depends(require_workflow_run_key),
):
    """Cancel a running or queued run in the caller's tenant."""
    return _wf.cancel_workflow_run(run_id=run_id, db=db, current_user=user)

@workflow_runner_router.post("/runs/{run_id}/restart", status_code=status.HTTP_200_OK)
def wr_restart_run(
        run_id: int,
        restart_request: schemas.WorkflowRestartRequest = None,
        db: Session = Depends(get_db),
        user: models.User = Depends(require_workflow_run_key),
):
    """Restart a finished run as a new run in the caller's tenant."""
    harbor_user = _wf.require_harbor_credentials(db=db, current_user=user)
    return _wf.restart_workflow_run(
        run_id=run_id,
        restart_request=restart_request,
        db=db,
        current_user=harbor_user,
        keycloak_token="",
    )

@workflow_runner_router.get("/runs/{run_id}/status")
def wr_run_status(
        run_id: int,
        db: Session = Depends(get_db),
        user: models.User = Depends(require_workflow_run_key),
):
    """DB-backed status for a run in the caller's tenant."""
    return _wf.get_workflow_run_status(run_id=run_id, db=db, current_user=user)

@workflow_runner_router.get("/runs/{run_id}/progress")
def wr_run_progress(
        run_id: int,
        db: Session = Depends(get_db),
        user: models.User = Depends(require_workflow_run_key),
):
    """Progress (percent complete, current node, ETA) for a run."""
    return _wf.get_workflow_progress(run_id=run_id, db=db, current_user=user)

@workflow_runner_router.get("/runs/{run_id}/node-status")
def wr_run_node_status(
        run_id: int,
        db: Session = Depends(get_db),
        user: models.User = Depends(require_workflow_run_key),
):
    """Per-node execution status for a run."""
    return _wf.get_node_execution_status(run_id=run_id, db=db, current_user=user)

@workflow_runner_router.get("/runs/{run_id}/cost")
def wr_run_cost(
        run_id: int,
        db: Session = Depends(get_db),
        user: models.User = Depends(require_workflow_run_key),
):
    """Per-node cost breakdown for a run."""
    return _wf.get_cost_breakdown_by_node(run_id=run_id, db=db, current_user=user)
