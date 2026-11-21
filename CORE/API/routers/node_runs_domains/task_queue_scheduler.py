# Auto-generated domain sub-module (Gate 2a split of routers/node_runs.py).
# Behavior-preserving: endpoints MOVED verbatim, grouped by audit domain.
from typing import List
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

import crud, models, schemas
from dependencies import get_db, require_active_and_onboarded_user, get_impersonated_user_from_service_account, require_service_account_token, require_bearer_or_api_key, get_tenant_minio_client
from utils.run_task_auth import node_or_service_principal, pin_run_task_tenant
from utils.minio_client import MinioClient

from routers.node_runs_domains._shared import *

from services import node_runs_task_queue_service as _tqs
from services._base import NotFoundError as _NotFoundError

service_router = APIRouter()


@service_router.get("/workflow/{run_id}/resources")
def get_node_run_resources_for_workflow_run_service(
    run_id: int,
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """
    Per-node-run resource usage for a workflow run.

    Returns one entry per node run with both:
      - Integrals from `task_resource_aggregate` (cpu_core_seconds,
        ram_gb_seconds, gpu_seconds, vram_gb_seconds, disk_read_mb,
        disk_write_mb, wall_clock_seconds).
      - Peaks from `task_resource_samples` (peak_cpu_percent,
        peak_ram_mb, peak_vram_mb, peak_gpu_util_pct, peak_gpu_temp_c).

    Used by ReturnExport (and other export containers) to write a rich
    workflow_info.json so users can see actual compute usage per stage.
    """
    try:
        return _tqs.get_run_resource_summary(db, run_id, principal)
    except _NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)

@service_router.get("/workflow/{workflow_run_id}/node/{node_id}", response_model=schemas.NodeRun)
def get_node_run_by_node_id(
    workflow_run_id: int,
    node_id: str,
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """
    Get a node run by workflow_run_id and graph_node_id.
    Service account endpoint for use by Traffic Cop DAG to get node_run_id.
    """
    try:
        return _tqs.get_node_run_by_node_id(db, workflow_run_id, node_id, principal)
    except _NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)

@service_router.patch("/workflow/{workflow_run_id}/node/{node_id}", response_model=schemas.NodeRun)
def update_node_run_by_node_id(
    workflow_run_id: int,
    node_id: str,
    status_update: schemas.NodeRunStatusUpdate,
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """
    Update a node run by workflow_run_id and graph_node_id.
    Service account endpoint for use by Traffic Cop DAG.
    """
    try:
        return _tqs.update_node_run_by_node_id(
            db,
            workflow_run_id,
            node_id,
            principal,
            status=status_update.status,
            inputs=status_update.inputs,
            outputs=status_update.outputs,
            error_message=status_update.error_message,
        )
    except _NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
