"""
Calibration API — batch calibration runner + data export endpoints.

Platform admin only. Used for generating ML training data.
"""

import io
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Calibration"],
    prefix="/calibration",
)


# --- Response models (R3) ---------------------------------------------------
# Every field is Optional with a None default and extra keys are kept, so these
# document the wire shape without being able to reject or strip a response.
# Values read from ML sidecar JSON / JSONB columns are typed Any so nothing is
# coerced. Every route sets response_model_exclude_unset=True so keys a return
# path does not produce are never added as nulls.


class GenerateTestDataResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Optional[str] = None
    message: Optional[str] = None


class RunBatchCalibrationResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Optional[str] = None
    # Only on the error early-returns.
    message: Optional[str] = None
    total_nodes: Optional[int] = None
    completed_nodes: Optional[int] = None
    total_tasks: Optional[int] = None
    completed_tasks: Optional[int] = None
    current_node: Optional[str] = None
    nodes_dispatched: Optional[List[Any]] = None
    nodes_failed: Optional[List[Any]] = None


class CalibrationProgressResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    total_nodes: Optional[int] = None
    calibrated: Optional[int] = None
    calibrating: Optional[int] = None
    uncalibrated: Optional[int] = None
    calibrating_nodes: Optional[List[Any]] = None


class TriggerRetrainResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Optional[str] = None
    outcome: Optional[str] = None
    task_id: Optional[int] = None
    message: Optional[str] = None


class ModelStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    model_loaded: Optional[bool] = None
    # Only when no model is trained yet.
    message: Optional[str] = None
    latest_version: Optional[Any] = None
    latest_status: Optional[Any] = None
    latest_metrics: Optional[Any] = None
    latest_deployed_at: Optional[Any] = None
    production_version: Optional[Any] = None
    total_versions: Optional[int] = None


class CalibrationPlanSummaryResponse(BaseModel):
    """create_calibration_plan / restart_calibration_plan / cancel_calibration_plan."""
    model_config = ConfigDict(extra="allow")
    # Only on restart.
    cancelled_plan_id: Optional[int] = None
    id: Optional[int] = None
    status: Optional[str] = None
    target_in_flight: Optional[int] = None
    total_nodes: Optional[int] = None
    nodes_completed: Optional[int] = None
    nodes_in_progress: Optional[Any] = None
    created_at: Optional[str] = None


class CalibrationPlanEnvelopeResponse(BaseModel):
    """get_calibration_plan: {"plan": null} or {"plan": {...}}."""
    model_config = ConfigDict(extra="allow")
    plan: Optional[Dict[str, Any]] = None


def _require_platform_admin(current_user: models.User = Depends(require_active_and_onboarded_user)):
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return current_user


def _require_fixture_ready(db: Session, tenant_id: int) -> None:
    """PFM §5.1 — calibration plan gate: refuse to start a plan while the
    tenant's calibration fixtures are not ready (fixture_status != 'ready').
    Inert until the PFM.19 cutover flag (fixture_shared_bucket_enabled, env
    fallback MOD_FIXTURE_SHARED_BUCKET) is on — same gate the coordinator
    applies — so pre-cutover behaviour is unchanged. 409 with the tenant's
    fixture state (status/version/checked_at) in the message."""
    from services import platform_settings as _psr
    if not bool(_psr.get_setting("fixture_shared_bucket_enabled")):
        return
    from services import fixture_service
    if fixture_service.fixtures_ready(db, tenant_id):
        return
    tenant = db.get(models.Tenant, tenant_id)
    status = getattr(tenant, "fixture_status", None) or "pending"
    version = getattr(tenant, "fixture_version", None)
    checked = getattr(tenant, "fixture_checked_at", None)
    raise HTTPException(
        status_code=409,
        detail=(
            f"Calibration blocked: tenant {tenant_id} fixture_status={status} "
            f"(fixture_version={version}, fixture_checked_at={checked}). "
            "Replay the fixture migrator via "
            f"POST /admin/fixtures/{tenant_id}/migrate."
        ),
    )


@router.post("/generate-test-data", response_model=GenerateTestDataResponse, response_model_exclude_unset=True)
def generate_test_data(
    current_user: models.User = Depends(_require_platform_admin),
):
    """Generate synthetic test files for all input types at 3 size tiers.
    Uploads to MinIO calibration-data bucket. Runs in background."""
    from services.calibration_service import start_test_data_generation
    return start_test_data_generation()


@router.post("/run-batch", response_model=RunBatchCalibrationResponse, response_model_exclude_unset=True)
def run_batch_calibration(
    worker_name: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_platform_admin),
):
    """Dispatch calibration tasks for ALL node definitions.
    9 tasks per node (3 size tiers × 3 runs). Runs asynchronously."""
    from services.calibration_service import run_batch
    return run_batch(db, tenant_id=current_user.tenant_id, worker_name=worker_name)


@router.get("/progress", response_model=CalibrationProgressResponse, response_model_exclude_unset=True)
def get_calibration_progress(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_platform_admin),
):
    """Get current calibration progress across all nodes."""
    from services.calibration_service import get_progress
    return get_progress(db)


@router.get("/export", response_model=None, response_class=StreamingResponse)
def export_training_data(
    calibration_only: bool = Query(True, description="Only export calibration run data"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_platform_admin),
):
    """Export training data as Parquet file.
    Joins TaskResourceSample + HardwareProfile + task metadata."""
    from services._base import NotFoundError
    from services.calibration_service import export_training_dataframe

    try:
        df = export_training_dataframe(db, calibration_only=calibration_only)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)

    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="calibration_samples.parquet"'},
    )


@router.post("/retrain", response_model=TriggerRetrainResponse, response_model_exclude_unset=True)
def trigger_retrain(
    force: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_platform_admin),
):
    """Submit the model retraining DAG. Returns immediately with the
    queued task id; the trainer runs as a regular DAG node on a GPU
    worker (image: `ghcr.io/modtim/ml-retrain:latest`).

    Wrapped in a Redis SET NX lock so concurrent submissions (admin
    double-click, cron-triggered + manual overlap, multi-uvicorn-worker
    leader race) collapse to a single in-flight retrain. Lock
    auto-expires after `RETRAIN_LOCK_TTL_SECONDS` so a wedged retrain
    doesn't permanently block. The DB-side idempotency check inside
    `submit_retrain_run` is a second guard for the case where Redis is
    unreachable — if a PENDING/CLAIMED/RUNNING retrain task already
    exists, returns its id rather than queueing a duplicate.
    """
    from services.calibration_service import (
        RetrainLockHeldError,
        RetrainSubmissionError,
        RetrainUnavailableError,
        submit_retrain,
    )

    try:
        return submit_retrain(
            db,
            force=force,
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
        )
    except RetrainLockHeldError as e:
        raise HTTPException(status_code=409, detail=e.message)
    except RetrainUnavailableError as e:
        raise HTTPException(status_code=503, detail=e.message)
    except RetrainSubmissionError as e:
        raise HTTPException(status_code=500, detail=e.message)


@router.get("/model-status", response_model=ModelStatusResponse, response_model_exclude_unset=True)
def get_model_status(
    current_user: models.User = Depends(_require_platform_admin),
):
    """Get current ML model version, metrics, and shadow status."""
    from services.calibration_service import get_model_status as _get_model_status
    return _get_model_status()


# --- Coordinator-driven calibration plan -----------------------------------
# These endpoints back the new in-flight-bounded coordinator. The legacy
# /run-batch endpoint above is kept for now (it still works mechanically)
# but its fire-and-forget semantics are deprecated — a Plan is the right
# user-facing primitive.

from pydantic import BaseModel, Field


class _CreatePlanReq(BaseModel):
    target_in_flight: int = Field(default=30, ge=1, le=500)


@router.post("/plan", response_model=CalibrationPlanSummaryResponse, response_model_exclude_unset=True)
def create_calibration_plan(
    req: _CreatePlanReq,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_platform_admin),
):
    """Start a new calibration plan. Only one running plan per tenant
    at a time (DB partial-unique enforced). The APScheduler tick will
    pick it up on its next run and start dripping work."""
    from services._base import ConflictError
    from services.calibration_service import create_plan

    tenant_id = current_user.tenant_id or 1
    _require_fixture_ready(db, tenant_id)
    try:
        plan = create_plan(
            db,
            tenant_id=tenant_id,
            target_in_flight=req.target_in_flight,
        )
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=e.message)
    return {
        "id": plan.id,
        "status": plan.status,
        "target_in_flight": plan.target_in_flight,
        "total_nodes": plan.total_nodes,
        "nodes_completed": plan.nodes_completed,
        "nodes_in_progress": plan.nodes_in_progress,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
    }


@router.get("/plan", response_model=CalibrationPlanEnvelopeResponse, response_model_exclude_unset=True)
def get_calibration_plan(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_platform_admin),
):
    """Current plan state for the requesting tenant.

    Returns `{plan: null}` (HTTP 200) when no plan exists yet — "no
    plan" is a valid state of the resource, not a missing resource.
    Returns `{plan: {...}}` (HTTP 200) when a plan exists. Avoids the
    red-404 console noise the Dashboard would otherwise show on every
    Training-tab open.
    """
    from services.calibration_service import get_current_plan

    plan = get_current_plan(db, tenant_id=current_user.tenant_id or 1)
    if plan is None:
        return {"plan": None}
    return {
        "plan": {
            "id": plan.id,
            "status": plan.status,
            "target_in_flight": plan.target_in_flight,
            "total_nodes": plan.total_nodes,
            "nodes_completed": plan.nodes_completed,
            "nodes_failed": plan.nodes_failed,
            "nodes_in_progress": plan.nodes_in_progress,
            "last_tick_at": plan.last_tick_at.isoformat() if plan.last_tick_at else None,
            "last_tick_reason": plan.last_tick_reason,
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
            "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        }
    }


@router.post("/plan/{plan_id}/cancel", response_model=CalibrationPlanSummaryResponse, response_model_exclude_unset=True)
def cancel_calibration_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_platform_admin),
):
    """Move plan to 'cancelled' AND mark every PENDING calibration
    task as CANCELLED. Tasks already CLAIMED/RUNNING are left alone —
    they terminate naturally."""
    from services._base import NotFoundError
    from services.calibration_service import cancel_plan

    try:
        plan = cancel_plan(db, plan_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
    return {"id": plan.id, "status": plan.status}


@router.post("/plan/restart", response_model=CalibrationPlanSummaryResponse, response_model_exclude_unset=True)
def restart_calibration_plan(
    req: _CreatePlanReq,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_platform_admin),
):
    """Cancel the tenant's running plan (if any) and create a fresh one.

    Use case: a fixture or node-script fix has shipped while a plan is
    mid-run. The already-dispatched tasks were enqueued against the
    pre-fix code and most will keep failing. Restarting flushes those
    pending tasks and starts the plan over against the updated images
    + fixtures.

    Idempotent on the cancel side — if no plan is running, only the
    create step runs. Returns the new plan's row.
    """
    from services._base import ConflictError, ServiceError
    from services.calibration_service import restart_plan

    tenant_id = current_user.tenant_id or 1
    _require_fixture_ready(db, tenant_id)
    try:
        cancelled_id, plan = restart_plan(
            db, tenant_id=tenant_id, target_in_flight=req.target_in_flight,
        )
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=e.message)
    except ServiceError as e:
        # Shouldn't happen — restart_plan only raises this when a
        # confirmed-running plan unexpectedly fails to cancel. Bubble up
        # so the operator sees the failure rather than silently
        # creating a second plan they didn't expect.
        raise HTTPException(status_code=500, detail=e.message)

    return {
        "cancelled_plan_id": cancelled_id,
        "id": plan.id,
        "status": plan.status,
        "target_in_flight": plan.target_in_flight,
        "total_nodes": plan.total_nodes,
        "nodes_completed": plan.nodes_completed,
        "nodes_in_progress": plan.nodes_in_progress,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
    }
