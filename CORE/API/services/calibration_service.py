"""Calibration service — business logic behind routers/calibration.py.

Follows the services/_base.py layering convention (openspec:
stack-training-and-prewarm, D9): the router stays HTTP-only (parse input,
auth via its `_require_platform_admin` dependency, shape the response);
this module owns orchestration/policy; crud/calibration.py is the only
place `db.query` runs for the `CalibrationPlan` resource.

The legacy `/generate-test-data`, `/run-batch`, `/progress`, `/export` and
`/model-status` endpoints delegate to the dynamically-imported `CORE/ML`
scripts (`batch_calibrator`, `data_generator`, `data_exporter`) exactly as
the router did inline — that dispatch-and-return orchestration is business
logic, so it lives here now rather than in the router body. `/retrain` and
the `/plan*` endpoints delegate to `services.ml_retrain` and
`services.calibration_coordinator` respectively (unowned by this migration;
imported, not modified).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional, Tuple

from sqlalchemy.orm import Session

import models
from services._base import ServiceError, ConflictError, NotFoundError

logger = logging.getLogger(__name__)

# Path to CORE/ML from this file: services/ and routers/ are both one level
# under CORE/API, so the same "../../ML" hop the router used still resolves
# to CORE/ML from here.
_ML_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "ML")


class RetrainLockHeldError(ServiceError):
    """Another retrain submission already holds the Redis lock (router -> 409)."""

    def __init__(self, holder, expires_in_s):
        message = (
            f"Retrain already in progress (holder={holder}, "
            f"expires_in_s={expires_in_s}). Wait for it to finish or let "
            f"the lock expire."
        )
        super().__init__(message, code="conflict")


class RetrainUnavailableError(ServiceError):
    """The retrain DAG cannot be dispatched right now (router -> 503)."""

    def __init__(self, message: str):
        super().__init__(message, code="unavailable")


class RetrainSubmissionError(ServiceError):
    """`submit_retrain_run` itself raised (router -> 500)."""

    def __init__(self, message: str):
        super().__init__(message, code="internal_error")


def _retrain_lock_key() -> str:
    return "mod:retrain:lock"


def _retrain_lock_ttl_seconds() -> int:
    return int(os.getenv("RETRAIN_LOCK_TTL_SECONDS", "14400"))  # 4 hours default


# --- /generate-test-data -----------------------------------------------

def start_test_data_generation() -> dict:
    """Kick off synthetic calibration test-data generation (all input
    types, 3 size tiers) in a daemon thread; return immediately."""

    def _run():
        try:
            from utils.storage import get_service_client
            client = get_service_client()
            import sys
            sys.path.insert(0, _ML_DIR)
            from data_generator import generate_all
            results = generate_all(client)
            logger.info("Test data generation complete: %s", results)
        except Exception as e:
            logger.error("Test data generation failed: %s", e)

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "generating", "message": "Synthetic test data generation started in background"}


# --- /run-batch ----------------------------------------------------------

def run_batch(db: Session, *, tenant_id: Optional[int], worker_name: Optional[str]) -> dict:
    """Dispatch the fire-and-forget calibration matrix (9 tasks/node) for
    every node definition."""
    import sys
    sys.path.insert(0, _ML_DIR)
    from batch_calibrator import run_batch_calibration as _run
    return _run(db, tenant_id, worker_name)


# --- /progress -------------------------------------------------------------

def get_progress(db: Session) -> dict:
    """Calibration progress across all nodes."""
    import sys
    sys.path.insert(0, _ML_DIR)
    from batch_calibrator import get_calibration_progress as _progress
    return _progress(db)


# --- /export ---------------------------------------------------------------

def export_training_dataframe(db: Session, *, calibration_only: bool):
    """Training data as a pandas DataFrame (TaskResourceSample joined with
    HardwareProfile + task metadata). Raises NotFoundError if empty."""
    import sys
    sys.path.insert(0, _ML_DIR)
    from data_exporter import export_training_data as _export

    df = _export(db, calibration_only=calibration_only)
    if df.empty:
        raise NotFoundError("No training data found. Run calibration first.")
    return df


# --- /retrain ----------------------------------------------------------

def submit_retrain(db: Session, *, force: bool, tenant_id: Optional[int], user_id) -> dict:
    """Acquire the Redis retrain lock (best-effort — proceeds lockless if
    Redis is unreachable) and submit the retrain DAG via
    `services.ml_retrain.submit_retrain_run`.

    Raises `RetrainLockHeldError` (409), `RetrainUnavailableError` (503,
    node not seeded / no tenant available) or `RetrainSubmissionError`
    (500, submission itself raised).
    """
    from services.ml_retrain import submit_retrain_run

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    rdb = None
    try:
        import redis
        rdb = redis.from_url(redis_url)
        acquired = rdb.set(
            _retrain_lock_key(), str(user_id), nx=True, ex=_retrain_lock_ttl_seconds()
        )
        if not acquired:
            existing_holder = rdb.get(_retrain_lock_key())
            existing_ttl = rdb.ttl(_retrain_lock_key())
            raise RetrainLockHeldError(existing_holder, existing_ttl)
    except RetrainLockHeldError:
        raise
    except Exception as e:
        logger.warning("Redis unavailable for retrain lock — proceeding without it: %s", e)
        rdb = None

    def _release_lock():
        if rdb is not None:
            try:
                rdb.delete(_retrain_lock_key())
            except Exception:
                pass

    try:
        task, outcome = submit_retrain_run(
            db,
            force=force,
            trigger_source="manual",
            tenant_id=tenant_id,
            user_id=user_id,
        )
    except Exception as e:
        _release_lock()
        logger.exception("submit_retrain_run failed")
        raise RetrainSubmissionError(f"Retrain submission failed: {e}")

    if outcome == "no_node":
        _release_lock()
        raise RetrainUnavailableError("ml_retrain NodeDefinition is not seeded. Run the seed job first.")
    if outcome == "no_tenant":
        _release_lock()
        raise RetrainUnavailableError("No active tenant available to dispatch retrain task.")

    return {
        "status": "started" if outcome == "submitted" else "in_flight",
        "outcome": outcome,
        "task_id": task.id if task is not None else None,
        "message": (
            "Retrain DAG submitted; runs on a GPU worker as a low-priority task."
            if outcome == "submitted"
            else "Retrain task already in flight; returning existing run."
        ),
    }


# --- /model-status -----------------------------------------------------

def get_model_status() -> dict:
    """Current ML model version, metrics, and shadow status, read from the
    scheduler model directory's `scheduler-v*.json` sidecars."""
    import json
    model_dir = os.path.join(_ML_DIR, "models")

    if not os.path.exists(model_dir):
        return {"model_loaded": False, "message": "No models trained yet"}

    versions = []
    for f in os.listdir(model_dir):
        if f.endswith(".json") and f.startswith("scheduler-v"):
            path = os.path.join(model_dir, f)
            with open(path) as fh:
                meta = json.load(fh)
                meta["file"] = f
                versions.append(meta)

    if not versions:
        return {"model_loaded": False, "message": "No models trained yet"}

    versions.sort(key=lambda x: x.get("version", 0), reverse=True)
    latest = versions[0]
    production = next((v for v in versions if v.get("status") == "production"), None)

    return {
        "model_loaded": True,
        "latest_version": latest.get("version"),
        "latest_status": latest.get("status"),
        "latest_metrics": latest.get("metrics"),
        "latest_deployed_at": latest.get("deployed_at"),
        "production_version": production.get("version") if production else None,
        "total_versions": len(versions),
    }


# --- /plan* (coordinator-driven calibration plan) ---------------------

def create_plan(db: Session, *, tenant_id: int, target_in_flight: int) -> models.CalibrationPlan:
    """Start a new calibration plan (router -> 409 on ConflictError)."""
    from services.calibration_coordinator import create_plan as _create_plan

    try:
        return _create_plan(db, tenant_id=tenant_id, target_in_flight=target_in_flight)
    except ValueError as e:
        raise ConflictError(str(e)) from e


def get_current_plan(db: Session, *, tenant_id: int) -> Optional[models.CalibrationPlan]:
    """Most recent plan for the tenant, or None if none exists yet."""
    import crud.calibration as calibration_crud

    return calibration_crud.get_latest_plan_for_tenant(db, tenant_id)


def cancel_plan(db: Session, plan_id: int) -> models.CalibrationPlan:
    """Cancel a plan + its PENDING tasks (router -> 404 on NotFoundError)."""
    from services.calibration_coordinator import cancel_plan as _cancel_plan

    try:
        return _cancel_plan(db, plan_id)
    except ValueError as e:
        raise NotFoundError(str(e)) from e


def restart_plan(
    db: Session, *, tenant_id: int, target_in_flight: int
) -> Tuple[Optional[int], models.CalibrationPlan]:
    """Cancel the tenant's running plan (if any) and create a fresh one.

    Returns `(cancelled_plan_id, new_plan)`. Raises `ServiceError` (router
    -> 500) if a confirmed-running plan unexpectedly fails to cancel, or
    `ConflictError` (router -> 409) if plan creation itself conflicts.
    """
    import crud.calibration as calibration_crud
    from services.calibration_coordinator import cancel_plan as _cancel_plan, create_plan as _create_plan

    running = calibration_crud.get_running_plan_for_tenant(db, tenant_id)
    cancelled_id = None
    if running is not None:
        try:
            _cancel_plan(db, running.id)
            cancelled_id = running.id
        except ValueError as e:
            # Shouldn't happen — we just confirmed it exists. Bubble up so
            # the operator sees the failure rather than silently creating a
            # second plan they didn't expect.
            raise ServiceError(str(e), code="internal_error") from e

    try:
        plan = _create_plan(db, tenant_id=tenant_id, target_in_flight=target_in_flight)
    except ValueError as e:
        raise ConflictError(str(e)) from e

    return cancelled_id, plan
