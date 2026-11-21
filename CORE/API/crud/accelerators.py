"""
CRUD layer for the accelerators admin router (Dashboard Accelerators panel +
LLM-generated benchmark-strategy review UI).

The ONLY place `db.query`/raw SQL for `routers/accelerators.py` lives (Gate 2b
service/CRUD migration). See `services/accelerators_service.py` for the policy
layer built on top.

NOTE: distinct from `crud/accelerator_cud.py`, which is owned by the
heartbeat/benchmark-dispatch workstream (routers/worker_management.py,
routers/worker_tasks.py, services/scheduler.py) and upserts/queries
AcceleratorProfile rows for that purpose. This module serves only the
read/admin endpoints in routers/accelerators.py — do not merge the two.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from models import (
    AcceleratorBenchmarkStrategy,
    AcceleratorProfile,
    HardwareProfile,
    WorkerLicense,
    WorkflowRun,
)


def get_worker_by_name(db: Session, worker_name: str) -> Optional[WorkerLicense]:
    """Fetch a WorkerLicense row by its unique worker_name."""
    return db.query(WorkerLicense).filter(
        WorkerLicense.worker_name == worker_name,
        WorkerLicense.deleted_at.is_(None),
    ).first()


def get_hardware_profile(db: Session, hardware_profile_id: int) -> Optional[HardwareProfile]:
    """Fetch a HardwareProfile row by id."""
    return db.query(HardwareProfile).filter(
        HardwareProfile.id == hardware_profile_id
    ).first()


def list_accelerator_profiles(db: Session, hardware_profile_id: int) -> List[AcceleratorProfile]:
    """All AcceleratorProfile rows for a hardware profile, ordered kind/index."""
    return db.query(AcceleratorProfile).filter(
        AcceleratorProfile.hardware_profile_id == hardware_profile_id
    ).order_by(AcceleratorProfile.kind, AcceleratorProfile.index).all()


def get_active_profiling_run(db: Session, hardware_profile_id: int) -> Optional[WorkflowRun]:
    """Most recent WorkflowRun actively profiling this hardware profile, if any."""
    return db.query(WorkflowRun).filter(
        WorkflowRun.parameters["is_profiling"].as_boolean() == True,
        WorkflowRun.parameters["hardware_profile_id"].as_integer() == hardware_profile_id,
    ).order_by(WorkflowRun.id.desc()).first()


class _TaskRow:
    """Attribute-access wrapper for a raw `api_task_queue` row tuple."""

    def __init__(self, row):
        self.id = row[0]
        self.status = type("S", (), {"value": row[1]})()
        self.task_payload = row[2] if isinstance(row[2], dict) else {}
        self.created_at = row[3]
        self.claimed_at = row[4]
        self.started_at = row[5]
        self.completed_at = row[6]
        self.error_message = row[7]


def list_benchmark_tasks(
    db: Session, worker_name: str, *, run_id: Optional[int] = None
) -> List[_TaskRow]:
    """Raw hardware/accelerator benchmark task rows for a worker, newest first.

    Optionally scoped to a single WorkflowRun (`run_id`) when a profiling run
    is active for the target hardware profile.
    """
    run_filter = ""
    params: Dict[str, Any] = {"worker": worker_name}
    if run_id is not None:
        run_filter = "AND workflow_run_id = :run_id"
        params["run_id"] = run_id

    rows = db.execute(sa_text(f"""
        SELECT id, status, task_payload, created_at, claimed_at, started_at, completed_at, error_message
        FROM api_task_queue
        WHERE assigned_to_worker = :worker
          AND task_payload->>'node_kind' IN ('hardware_benchmark', 'accelerator_benchmark')
          {run_filter}
        ORDER BY id DESC
    """), params).fetchall()
    return [_TaskRow(r) for r in rows]


def get_accelerator_profile(db: Session, accelerator_id: int) -> Optional[AcceleratorProfile]:
    """Fetch one AcceleratorProfile row by id."""
    return db.query(AcceleratorProfile).filter(
        AcceleratorProfile.id == accelerator_id
    ).first()


def list_strategies(db: Session) -> List[AcceleratorBenchmarkStrategy]:
    """All benchmark strategies, ordered kind/id."""
    return db.query(AcceleratorBenchmarkStrategy).order_by(
        AcceleratorBenchmarkStrategy.kind,
        AcceleratorBenchmarkStrategy.id,
    ).all()


def get_strategy(db: Session, strategy_id: int) -> Optional[AcceleratorBenchmarkStrategy]:
    """Fetch one benchmark strategy row by id."""
    return db.query(AcceleratorBenchmarkStrategy).filter(
        AcceleratorBenchmarkStrategy.id == strategy_id
    ).first()


def delete_strategy(db: Session, strategy: AcceleratorBenchmarkStrategy) -> None:
    """Soft-delete (reject) a non-seeded strategy row.

    iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    iso-no-hard-delete). `AcceleratorBenchmarkStrategy` now carries a
    `deleted_at` column (SoftDeleteMixin) and is in database.py's soft-delete
    listener, so tombstoned strategies drop out of `list_strategies` /
    `get_strategy` automatically.

    FOLLOW-UP (services agent): the strategy generator
    (services/benchmark_strategy_generator.py) inserts a fresh row on each
    benchmark; the unique constraint (kind, vendor_id, device_id_pattern) is a
    plain constraint, so re-benchmarking a device whose prior strategy was
    rejected will collide with the tombstone. That create path needs
    reactivation-on-recreate (look up include_deleted and clear deleted_at)
    once it is migrated.
    """
    from datetime import datetime, timezone
    strategy.deleted_at = datetime.now(timezone.utc)
