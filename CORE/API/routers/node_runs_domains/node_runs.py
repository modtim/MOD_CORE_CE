# Auto-generated domain sub-module (Gate 2a split of routers/node_runs.py).
# Behavior-preserving: endpoints MOVED verbatim, grouped by audit domain.
# Gate 2b: handler bodies now delegate to services/node_runs_service.py +
# crud/node_runs.py (query layer). Signatures/decorators/docstrings/tags are
# UNCHANGED so the contract snapshot stays byte-identical.
from typing import List
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

import crud, models, schemas
from dependencies import (
    get_db,
    get_tenant_minio_client,
    get_impersonated_user_from_service_account,
    require_service_account_token,
    require_bearer_or_api_key,
    require_active_and_onboarded_user,
    # GY.C16 follow-up: auditor data-content gate on run-output routes.
    require_auditor_data_content,
)
from utils.run_task_auth import node_or_service_principal, pin_run_task_tenant
from utils.minio_client import MinioClient

from routers.node_runs_domains._shared import *
from services import node_runs_service, run_result_service
from services._base import NotFoundError

router = APIRouter()


@router.patch("/{node_run_id}/status", response_model=schemas.NodeRun)
def update_node_run_status(
    node_run_id: int,
    status_update: schemas.NodeRunStatusUpdate,
    db: Session = Depends(get_db),
    # This endpoint is called by worker containers, so it uses the impersonation dependency
    current_user: models.User = Depends(get_impersonated_user_from_service_account),
):
    """
    Update the status of a specific node run.
    This is intended to be called by the workflow execution engine.
    """
    try:
        return node_runs_service.update_node_run_status(
            db,
            node_run_id=node_run_id,
            tenant_id=current_user.tenant_id,
            status_update=status_update,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)

@router.get("/{run_id}/previews",
            dependencies=[Depends(require_auditor_data_content)])
def get_node_run_previews(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
):
    """
    Get presigned preview URLs for all completed node outputs in a workflow run.

    Returns a mapping of graph_node_id -> list of preview objects with presigned URLs,
    file extension, and asset category (image, model3d, video, other).
    """
    try:
        return node_runs_service.list_run_previews(
            db,
            run_id=run_id,
            tenant_id=current_user.tenant_id,
            user=current_user,
            minio_client=minio_client,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)

@router.get("/{run_id}/fanout-summary")
def get_run_fanout_summary(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Per-node fan-out width for a run, so the canvas can badge fanned edges.

    Fan-out is one task per distinct input FILE (source cog). A re-run of a node
    enqueues another task for the SAME source cog (a new attempt), so counting
    raw task rows would wrongly badge a re-run as a fan-out. We therefore count
    the number of DISTINCT source cogs per node — re-run attempts collapse to the
    file they re-ran. A node whose distinct-source count > 1 truly fanned out.
    Returns {"node_task_counts": {graph_node_id: count}}.
    """
    try:
        return node_runs_service.get_run_fanout_summary(
            db,
            run_id=run_id,
            tenant_id=current_user.tenant_id,
            user=current_user,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)

@router.get("/{run_id}/node-fanout/{graph_node_id}",
            dependencies=[Depends(require_auditor_data_content)])
def get_node_fanout(
    run_id: int,
    graph_node_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
):
    """Exact per-file input↔output pairing for one fanned node in a run.

    Each fanned task carries a single source cog (ProjectInfo.cog_ids[0]) — the
    exact lineage key. Returns one item per task:
      {cog_id, name, status, input (source cog file), output (task result)}
    ordered by cog id for a stable list. Poll for live status/output updates.
    """
    try:
        return node_runs_service.get_node_fanout(
            db,
            run_id=run_id,
            graph_node_id=graph_node_id,
            tenant_id=current_user.tenant_id,
            user=current_user,
            minio_client=minio_client,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)

# GY.C16 follow-up: NOT guarded — this route is deliberately Bearer-OR-API-key
# (external apps poll runs with X-API-Key and have no Authorization header),
# so the guard's Bearer funnel would 401 normal API-key callers. Auditor
# data-content enforcement for run outputs lives on /previews, /node-fanout
# and /node-versions above.
@router.get("/{run_id}/result", response_model=schemas.RunResultResponse,
            responses={404: {"description": "Run not found, or not visible to the caller (tenant/RBAC); never 403, no existence oracle"}})
def get_run_result(
    run_id: int,
    db: Session = Depends(get_db),
    # Auth: Bearer OR API-key — deliberately NOT the browser-only
    # require_active_and_onboarded_user, because external apps built on MOD
    # poll their runs with a tenant API key. The view gate (tenant scope +
    # per-workflow RBAC, same as the neighbouring run-detail routes) lives in
    # run_result_service and denies with 404.
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """One call for an external app to get a finished (or in-progress)
    workflow run's outcome: status + timing, the error with its failed node,
    every node's outputs (fan-out included, per NodeRun), the final outputs
    of the sink nodes (the "answer") with a presigned download url, and — for
    small text finals (<= 64 KB) — the content inlined as ``text``.

    Tenant-scoped: a run in another tenant is a 404, not a 403.
    """
    # Built from the resolved user (NOT the get_tenant_minio_client dependency,
    # which is bound to the Bearer-only get_current_user and would 401 an
    # X-API-Key caller). BYO-S3 / tenant-bucket resolution happens inside the
    # client's constructor via utils.storage.resolve_backend.
    minio_client = MinioClient(tenant_id=current_user.tenant_id)
    try:
        return run_result_service.get_run_result(
            db,
            run_id=run_id,
            tenant_id=current_user.tenant_id,
            user=current_user,
            minio_client=minio_client,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)


@router.get("/{run_id}/node-versions/{graph_node_id}",
            dependencies=[Depends(require_auditor_data_content)])
def get_node_versions(
    run_id: int,
    graph_node_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    minio_client: MinioClient = Depends(get_tenant_minio_client),
):
    """node-run-controls (keep+version) — every attempt of a node in a run, kept
    and downloadable, newest first. Re-runs never overwrite: each attempt is its
    own task/output_namespace, so you can open any past attempt's outputs to see
    what went wrong. Nothing is ever deleted.
    """
    try:
        return node_runs_service.get_node_versions(
            db,
            run_id=run_id,
            graph_node_id=graph_node_id,
            tenant_id=current_user.tenant_id,
            user=current_user,
            minio_client=minio_client,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)
