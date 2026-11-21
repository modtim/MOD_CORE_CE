"""
Service layer for the accelerators admin router (Dashboard Accelerators panel
+ LLM-generated benchmark-strategy review UI).

Owns policy (platform-admin gate) and response shaping; delegates all data
access to `crud/accelerators.py`. Migrated out of `routers/accelerators.py`
(Gate 2b service/CRUD migration) — see that module for the thin HTTP layer.

Follows the `services/_base.py` convention: functions raise `ServiceError`
subclasses (never `HTTPException`); the router maps them to HTTP status
codes. `services/_base.py` ships `NotFoundError`/`ConflictError` but no
403 — this domain declares its own `ForbiddenError` per the migration's
shared rules.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

import crud.accelerators as accelerators_crud
from models import TaskStatus, User
from services._base import NotFoundError, ServiceError

logger = logging.getLogger(__name__)


class ForbiddenError(ServiceError):
    """Caller is not a platform admin (router maps -> 403)."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


def _require_admin(current_user: User) -> None:
    if not current_user.is_platform_admin:
        raise ForbiddenError("Platform admin only")


def get_worker_accelerators(db: Session, worker_name: str, current_user: User) -> Dict[str, Any]:
    """Per-device accelerator profiles + live benchmark progress for a worker."""
    _require_admin(current_user)

    worker = accelerators_crud.get_worker_by_name(db, worker_name)
    if not worker:
        raise NotFoundError("Worker not found")
    if not worker.hardware_profile_id:
        return {"worker_name": worker_name, "accelerators": [], "profiling_status": "no_profile"}

    profile = accelerators_crud.get_hardware_profile(db, worker.hardware_profile_id)
    accels = accelerators_crud.list_accelerator_profiles(db, worker.hardware_profile_id)
    active_run = accelerators_crud.get_active_profiling_run(db, worker.hardware_profile_id)

    run_id = active_run.id if active_run else None
    benchmark_tasks = accelerators_crud.list_benchmark_tasks(db, worker_name, run_id=run_id)

    # Map tasks to devices by TARGET_KIND + TARGET_INDEX
    task_map: Dict[str, Any] = {}
    system_task = None
    for t in benchmark_tasks:
        env = (t.task_payload or {}).get("container_env", {})
        kind = env.get("TARGET_KIND", "").strip().lower()
        index = env.get("TARGET_INDEX", "")
        node_kind_value = (t.task_payload or {}).get("node_kind", "")
        if node_kind_value == "hardware_benchmark":
            if system_task is None or t.id > system_task.id:
                system_task = t
        elif kind:
            key = f"{kind}:{index}"
            if key not in task_map or t.id > task_map[key].id:
                task_map[key] = t

    # Only devices whose node_id is actually part of the currently active
    # profiling run should be shown as "Waiting to dispatch". A single-node
    # run (e.g. only=cuda:0) must NOT make every other device look queued.
    plan_node_ids: set[str] = set()
    if active_run and active_run.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
        plan_nodes = ((active_run.parameters or {}).get("execution_plan", {}) or {}).get("nodes", {}) or {}
        plan_node_ids = set(plan_nodes.keys())

    def _task_progress(task, own_node_id: Optional[str] = None):
        if not task:
            # No task row for this device. If this device is a node in the
            # live run's plan, it's genuinely queued behind the active one;
            # otherwise it's idle and should show nothing.
            if own_node_id and own_node_id in plan_node_ids:
                return {"step": "not_dispatched", "label": "Waiting to dispatch", "elapsed_s": None}
            return None
        now = datetime.now(timezone.utc) if task.completed_at is None else task.completed_at
        status = task.status.value if hasattr(task.status, 'value') else str(task.status)
        if status in ("COMPLETED",):
            elapsed = (task.completed_at - task.created_at).total_seconds() if task.completed_at and task.created_at else 0
            return {"step": "completed", "label": "Completed", "elapsed_s": round(elapsed, 1)}
        if status in ("FAILED",):
            return {"step": "failed", "label": f"Failed: {task.error_message or 'unknown'}", "elapsed_s": None}
        if status in ("RUNNING",):
            elapsed = (now - (task.started_at or task.claimed_at or task.created_at)).total_seconds()
            return {"step": "running", "label": "Running benchmark...", "elapsed_s": round(elapsed, 1)}
        if status in ("CLAIMED",):
            elapsed = (now - (task.claimed_at or task.created_at)).total_seconds()
            return {"step": "pulling", "label": "Claimed — pulling container image...", "elapsed_s": round(elapsed, 1)}
        if status in ("ASSIGNED",):
            elapsed = (now - task.created_at).total_seconds()
            return {"step": "assigned", "label": "Assigned — waiting for worker to claim", "elapsed_s": round(elapsed, 1)}
        if status in ("PENDING",):
            elapsed = (now - task.created_at).total_seconds()
            return {"step": "queued", "label": "Queued — waiting for available worker", "elapsed_s": round(elapsed, 1)}
        return {"step": "unknown", "label": status, "elapsed_s": None}

    total = len(accels)
    completed = sum(1 for a in accels if a.benchmark_status == "completed")
    # 'unsupported' is a terminal outcome like 'failed'/'skipped' — count it here
    # so a quarantined device doesn't sit forever in the "pending" bucket.
    failed = sum(1 for a in accels if a.benchmark_status in ("failed", "skipped", "unsupported"))

    return {
        "worker_name": worker_name,
        "hardware_profile_id": worker.hardware_profile_id,
        "profiling_status": profile.benchmark_status if profile else "pending",
        "progress": {
            "total": total + 1,  # +1 for system benchmark
            "completed": completed + (1 if system_task and system_task.status.value == "COMPLETED" else 0),
            "failed": failed + (1 if system_task and system_task.status.value == "FAILED" else 0),
            "pending": (total - completed - failed) + (1 if not system_task or system_task.status.value not in ("COMPLETED", "FAILED") else 0),
        },
        "system_benchmark": {
            # `_task_progress` returns None when the worker has no benchmark
            # task row yet (fresh worker, before the first dispatch). The
            # accelerator-card consumer handles None directly, but the
            # system-card consumer dict-unpacks the result and would crash
            # with `TypeError: NoneType is not a mapping`. Fall back to an
            # explicit idle progress shape so the response is well-formed.
            **(_task_progress(
                system_task,
                own_node_id=f"benchmark-system-{worker.hardware_profile_id}",
            ) or {"step": "idle", "label": "Idle", "elapsed_s": None}),
            # Scores inline so the dashboard SYSTEM card can render
            # CPU/Memory/I/O/MinIO/Docker numbers the same way
            # accelerator cards render compute/mem_bw/copy_bw. Pulled
            # from the HardwareProfile which holds the System result.
            # Nullable fields (MinIO, docker_pull, container_startup)
            # stay null when the test was skipped/failed.
            "scores": {
                "cpu_multi_score": profile.cpu_multi_score if profile else None,
                "cpu_single_score": profile.cpu_single_score if profile else None,
                "memory_bw_score": profile.memory_bw_score if profile else None,
                "io_seq_score": profile.io_seq_score if profile else None,
                "io_random_score": profile.io_random_score if profile else None,
                "disk_seq_read_score": profile.disk_seq_read_score if profile else None,
                "minio_download_mbps": profile.minio_download_mbps if profile else None,
                "minio_upload_mbps": profile.minio_upload_mbps if profile else None,
                "docker_pull_seconds": profile.docker_pull_seconds if profile else None,
                "container_startup_ms": profile.container_startup_ms if profile else None,
            } if profile else None,
        },
        "accelerators": [
            {
                "id": a.id,
                "kind": a.kind,
                "index": a.index,
                "name": a.name,
                "vendor_id": a.vendor_id,
                "device_id": a.device_id,
                "uuid": a.uuid,
                "vram_gb": a.vram_gb,
                "driver_version": a.driver_version,
                "compute_score": a.compute_score,
                "memory_bw_score": a.memory_bw_score,
                "copy_bw_score": a.copy_bw_score,
                "benchmark_status": a.benchmark_status,
                "admin_disabled": a.admin_disabled,
                "schedulable": (not a.admin_disabled) and a.benchmark_status not in ("failed", "unsupported"),
                "benchmarked_at": a.benchmarked_at.isoformat() if a.benchmarked_at else None,
                "error_message": a.error_message,
                "is_integrated": a.is_integrated,
                "shared_ram_usage_gb": a.shared_ram_usage_gb,
                "cpu_bw_contention": a.cpu_bw_contention,
                "task_progress": _task_progress(
                    task_map.get(f"{a.kind}:{a.index}"),
                    own_node_id=f"benchmark-{worker.hardware_profile_id}-{a.kind}-{a.index}",
                ),
            }
            for a in accels
        ],
    }


def set_accelerator_enabled(db: Session, accelerator_id: int, body: Any, current_user: User) -> Dict[str, Any]:
    """One-click operator quarantine — disable/enable a single accelerator.

    `body` duck-types the router's `AcceleratorEnabledRequest` (`.enabled`,
    `.reason`).
    """
    _require_admin(current_user)

    accel = accelerators_crud.get_accelerator_profile(db, accelerator_id)
    if not accel:
        raise NotFoundError("Accelerator not found")

    accel.admin_disabled = not body.enabled
    if not body.enabled:
        # Record why, so the UI can show the operator's note next to the badge.
        note = (body.reason or "Disabled by operator").strip()[:500]
        accel.error_message = note
    else:
        # Re-enabling: clear an operator note ONLY if it was our disable note;
        # keep a real benchmark error message intact.
        if accel.benchmark_status not in ("failed", "unsupported"):
            accel.error_message = None
    db.commit()

    logger.info(
        "Accelerator %d (%s[%s]) admin_disabled=%s by user %s",
        accel.id, accel.kind, accel.index, accel.admin_disabled, current_user.id,
    )
    return {
        "success": True,
        "accelerator_id": accel.id,
        "kind": accel.kind,
        "index": accel.index,
        "admin_disabled": accel.admin_disabled,
        "benchmark_status": accel.benchmark_status,
        "message": (
            f"Accelerator {accel.kind}[{accel.index}] "
            f"{'disabled — excluded from scheduling' if accel.admin_disabled else 'enabled'}."
        ),
    }


def list_strategies(db: Session, current_user: User):
    """List all benchmark strategies. Admin review UI."""
    _require_admin(current_user)

    strategies = accelerators_crud.list_strategies(db)

    return [
        {
            "id": s.id,
            "kind": s.kind,
            "vendor_id": s.vendor_id,
            "device_id_pattern": s.device_id_pattern,
            "device_family": s.device_family,
            "image_ref": s.image_ref,
            "source": s.source,
            "auto_approved": s.auto_approved,
            "runs_succeeded": s.runs_succeeded,
            "runs_failed": s.runs_failed,
            "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
            "last_anomaly_at": s.last_anomaly_at.isoformat() if s.last_anomaly_at else None,
            "notes": s.notes,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in strategies
    ]


def approve_strategy(db: Session, strategy_id: int, current_user: User) -> Dict[str, Any]:
    """Manually approve an LLM-generated strategy."""
    _require_admin(current_user)

    strategy = accelerators_crud.get_strategy(db, strategy_id)
    if not strategy:
        raise NotFoundError("Strategy not found")

    strategy.auto_approved = True
    strategy.approved_by_user_id = current_user.id
    db.commit()
    return {"success": True, "message": f"Strategy {strategy_id} ({strategy.kind}) approved."}


def reject_strategy(db: Session, strategy_id: int, current_user: User) -> Dict[str, Any]:
    """Reject and delete an LLM-generated strategy. Seeded strategies can't be rejected."""
    _require_admin(current_user)

    strategy = accelerators_crud.get_strategy(db, strategy_id)
    if not strategy:
        raise NotFoundError("Strategy not found")
    if strategy.source == "seeded":
        raise ServiceError("Cannot reject a seeded strategy.", code="bad_request")

    accelerators_crud.delete_strategy(db, strategy)
    db.commit()
    return {"success": True, "message": f"Strategy {strategy_id} ({strategy.kind}) rejected and deleted."}
