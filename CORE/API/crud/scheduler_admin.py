"""
CRUD helpers for the predictive overlap scheduler admin endpoints
(routers/scheduler_admin.py).

The ONLY place `db.query` lives for this domain: pattern-database stats,
task-queue stats, worker health, and per-worker timelines all read straight
off `ResourcePattern` / `ApiTaskQueue` / `WorkerLicense` /
`WorkerResourceTimeline`. These are platform-admin-only observability reads
across the whole scheduler (not a single tenant's data — the router's
`require_platform_admin` dependency gates access), so there is no
`tenant_id` scoping here.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import (
    ApiTaskQueue, ApiTaskStatus,
    ResourcePattern,
    WorkerLicense, WorkerResourceTimeline, WorkerStatus,
)


def get_pattern_counts(db: Session) -> Tuple[int, float, int]:
    """(total_patterns, avg_confidence, high_confidence_count [confidence >= 0.7])."""
    pattern_count = db.query(func.count(ResourcePattern.id)).scalar()
    avg_confidence = db.query(func.avg(ResourcePattern.confidence)).scalar() or 0
    high_confidence = db.query(func.count(ResourcePattern.id)).filter(
        ResourcePattern.confidence >= 0.7
    ).scalar()
    return pattern_count, avg_confidence, high_confidence


def get_task_queue_counts(db: Session) -> Tuple[int, int, int]:
    """(pending, assigned, running) counts on ApiTaskQueue."""
    pending = db.query(func.count(ApiTaskQueue.id)).filter(
        ApiTaskQueue.status == ApiTaskStatus.PENDING
    ).scalar()
    assigned = db.query(func.count(ApiTaskQueue.id)).filter(
        ApiTaskQueue.status == ApiTaskStatus.ASSIGNED
    ).scalar()
    running = db.query(func.count(ApiTaskQueue.id)).filter(
        ApiTaskQueue.status == ApiTaskStatus.RUNNING
    ).scalar()
    return pending, assigned, running


def count_placed_since(db: Session, since: datetime) -> int:
    """Tasks that left PENDING with a worker assigned, updated since `since`."""
    return db.query(func.count(ApiTaskQueue.id)).filter(
        ApiTaskQueue.status != ApiTaskStatus.PENDING,
        ApiTaskQueue.assigned_to_worker.isnot(None),
        ApiTaskQueue.updated_at >= since,
    ).scalar()


def count_rescheduled_since(db: Session, since: datetime) -> int:
    """Tasks with `scheduling_version > 0` (rescheduled at least once) created
    since `since`."""
    return db.query(func.count(ApiTaskQueue.id)).filter(
        ApiTaskQueue.scheduling_version > 0,
        ApiTaskQueue.created_at >= since,
    ).scalar()


def count_created_since(db: Session, since: datetime) -> int:
    """Total tasks created since `since` (denominator for the reschedule rate)."""
    return db.query(func.count(ApiTaskQueue.id)).filter(
        ApiTaskQueue.created_at >= since,
    ).scalar()


def list_completed_with_scheduling_metadata(
    db: Session, since: datetime, *, limit: int = 100
) -> List[ApiTaskQueue]:
    """Completed tasks with scheduling metadata, for the prediction-accuracy
    sample. Bounded — this is a diagnostic sample, not a full list."""
    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status == ApiTaskStatus.COMPLETED,
        ApiTaskQueue.scheduling_metadata.isnot(None),
        ApiTaskQueue.completed_at >= since,
    ).limit(limit).all()


def get_worker_health_counts(db: Session) -> Tuple[int, int, int]:
    """(total_licensed_workers, workers_with_snapshots, thermal_throttled_count)."""
    total_workers = db.query(func.count(WorkerLicense.id)).filter(
        WorkerLicense.deleted_at.is_(None),
        WorkerLicense.status == WorkerStatus.LICENSED,
    ).scalar()
    workers_with_snapshots = db.query(func.count(WorkerResourceTimeline.id)).scalar()
    thermal_throttled = db.query(func.count(WorkerResourceTimeline.id)).filter(
        WorkerResourceTimeline.thermal_throttled == True  # noqa: E712 (SQLAlchemy column comparator)
    ).scalar()
    return total_workers, workers_with_snapshots, thermal_throttled


def get_worker_by_name(db: Session, worker_name: str) -> Optional[WorkerLicense]:
    """Fetch one worker license by its `worker_name`. Not tenant-scoped:
    worker identity is platform-global."""
    return db.query(WorkerLicense).filter(
        WorkerLicense.deleted_at.is_(None),
        WorkerLicense.worker_name == worker_name,
    ).first()


def get_worker_snapshot(db: Session, worker_name: str) -> Optional[WorkerResourceTimeline]:
    """Latest resource-timeline snapshot row for a worker (no explicit
    ordering here — matches prior router behaviour, which took `.first()`)."""
    return db.query(WorkerResourceTimeline).filter(
        WorkerResourceTimeline.worker_name == worker_name
    ).first()


def list_active_tasks_for_worker(db: Session, worker_name: str) -> List[ApiTaskQueue]:
    """Tasks currently assigned/claimed/running on a specific worker."""
    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status.in_([ApiTaskStatus.ASSIGNED, ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING]),
        ApiTaskQueue.assigned_to_worker == worker_name,
    ).all()


def list_top_patterns(db: Session, *, limit: int = 100) -> List[ResourcePattern]:
    """Resource patterns ordered by confidence, bounded by `limit`."""
    return db.query(ResourcePattern).order_by(
        ResourcePattern.confidence.desc()
    ).limit(limit).all()


def list_patterns_for_node_kind(db: Session, node_kind: str) -> List[ResourcePattern]:
    """All patterns (one per queue/tier) for a given node_kind."""
    return db.query(ResourcePattern).filter(
        ResourcePattern.node_kind == node_kind
    ).all()
