"""tenant-resource-monitor — per-task resource aggregator + leak scan.

Computes ``TaskResourceAggregate`` rows from the ``TaskResourceSample``
stream the worker emits during execution, then scans the rollup for
per-resource leaks: each metric (GPU-sec, CPU-core-sec, RAM GB-sec,
IO MB) has its own ``native_unit_per_credit`` threshold, and the
scan fires a ``RESOURCE_LEAK_DETECTED`` event when ANY resource's
actual ratio crosses its threshold for a (tenant, user) over a 24h
window.

No dollarization — the operator sees *which* resource is being
abused, not a single rolled-up dollar number. Cost-rate guesses
were exposing operators to a number that pretended to be precise
while resting on assumed pricing; the native breakdown is honest.

Resource math (sample integral over sample intervals):
- ``gpu_seconds`` = Σ ``(gpu_util_pct / 100) * Δt`` — GPU utilization-seconds.
- ``cpu_core_seconds`` = Σ ``(cpu_percent / 100) * Δt`` — per-core, summed.
- ``ram_gb_seconds`` = Σ ``(ram_mb / 1024) * Δt`` — RAM-GB-seconds.
- ``vram_gb_seconds`` = Σ ``(vram_mb / 1024) * Δt`` — VRAM-GB-seconds.
- ``disk_read_mb`` / ``disk_write_mb`` = ``max(container_disk_*_mb)`` over the
  samples (the worker emits cumulative-from-start counters, so the last
  value is the total).
- ``wall_clock_seconds`` = ``completed_at - started_at`` if both present,
  else ``Σ Δt`` over samples, else 0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models

log = logging.getLogger(__name__)

# Single-sample fallback Δt — matches the worker's monitor batch
# interval. Without this a one-sample task is credited zero
# resource-seconds.
_DEFAULT_SAMPLE_INTERVAL_S = 30.0

# Stable mapping of the four resources we track for thresholds + UI
# pills. Keep in sync with the four columns on ResourceMonitorConfig
# and the four ratio fields on ResourceMonitorTenantRow / UserRow.
RESOURCE_KINDS = ("gpu", "cpu", "ram", "io")


@dataclass(frozen=True)
class _Totals:
    gpu_seconds: float = 0.0
    cpu_core_seconds: float = 0.0
    ram_gb_seconds: float = 0.0
    vram_gb_seconds: float = 0.0
    disk_read_mb: float = 0.0
    disk_write_mb: float = 0.0
    wall_clock_seconds: float = 0.0


def _get_config(db: Session) -> models.ResourceMonitorConfig:
    """Return the singleton resource_monitor_config row. Seeds
    defensively if startup migration somehow missed."""
    row = db.query(models.ResourceMonitorConfig).filter(
        models.ResourceMonitorConfig.singleton_id == 1
    ).one_or_none()
    if row is not None:
        return row
    row = models.ResourceMonitorConfig(singleton_id=1)
    db.add(row)
    db.flush()
    return row


def _user_id_for_task(db: Session, task: models.ApiTaskQueue) -> Optional[int]:
    """Resolve the human user attributable to a task.

    Workflow tasks → the WorkflowRun.triggered_by_user_id.
    Standalone tasks (calibration, benchmarks) → None.
    """
    if task.workflow_run_id is None:
        return None
    run = db.query(models.WorkflowRun).filter(
        models.WorkflowRun.id == task.workflow_run_id
    ).one_or_none()
    return getattr(run, "triggered_by_user_id", None) if run else None


def _compute_totals(samples: Sequence[models.TaskResourceSample], task: models.ApiTaskQueue) -> _Totals:
    """Integrate samples over their inter-sample intervals.

    Returns zeros if ``samples`` is empty (the task never produced
    telemetry — falls back to wall-clock derived from task timestamps).
    """
    if not samples:
        wall = 0.0
        if task.started_at and task.completed_at:
            wall = max(0.0, (task.completed_at - task.started_at).total_seconds())
        return _Totals(wall_clock_seconds=wall)

    ordered = sorted(samples, key=lambda s: s.timestamp)

    gpu_sec = 0.0
    cpu_core_sec = 0.0
    ram_gb_sec = 0.0
    vram_gb_sec = 0.0
    wall = 0.0

    for i, s in enumerate(ordered):
        if i + 1 < len(ordered):
            dt = (ordered[i + 1].timestamp - s.timestamp).total_seconds()
            if dt <= 0:
                dt = _DEFAULT_SAMPLE_INTERVAL_S
        else:
            dt = _DEFAULT_SAMPLE_INTERVAL_S
        dt = min(dt, 600.0)
        wall += dt

        gpu_util = (s.gpu_util_pct or 0.0) / 100.0
        cpu_util_cores = (s.cpu_percent or 0.0) / 100.0
        ram_gb = (s.ram_mb or 0.0) / 1024.0
        vram_gb = (s.vram_mb or 0.0) / 1024.0

        gpu_sec += max(0.0, gpu_util) * dt
        cpu_core_sec += max(0.0, cpu_util_cores) * dt
        ram_gb_sec += max(0.0, ram_gb) * dt
        vram_gb_sec += max(0.0, vram_gb) * dt

    disk_read = max((s.container_disk_read_mb or 0.0) for s in ordered)
    disk_write = max((s.container_disk_write_mb or 0.0) for s in ordered)

    if task.started_at and task.completed_at:
        wall = max(0.0, (task.completed_at - task.started_at).total_seconds())

    return _Totals(
        gpu_seconds=gpu_sec,
        cpu_core_seconds=cpu_core_sec,
        ram_gb_seconds=ram_gb_sec,
        vram_gb_seconds=vram_gb_sec,
        disk_read_mb=disk_read,
        disk_write_mb=disk_write,
        wall_clock_seconds=wall,
    )


def aggregate_task(
    db: Session, task_id: int
) -> tuple[Optional[models.TaskResourceAggregate], bool]:
    """Materialize one TaskResourceAggregate row from a task's samples.

    Idempotent — if a row already exists for ``task_id`` it is returned
    unchanged (``was_new = False``). Caller decides whether to commit.
    """
    existing = db.query(models.TaskResourceAggregate).filter(
        models.TaskResourceAggregate.task_id == task_id
    ).one_or_none()
    if existing is not None:
        return existing, False

    task = db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.id == task_id
    ).one_or_none()
    if task is None:
        log.warning("resource_monitor: task %s missing — skipping aggregate", task_id)
        return None, False

    samples = db.query(models.TaskResourceSample).filter(
        models.TaskResourceSample.task_id == task_id
    ).order_by(models.TaskResourceSample.timestamp.asc()).all()

    totals = _compute_totals(samples, task)

    agg = models.TaskResourceAggregate(
        task_id=task.id,
        tenant_id=task.tenant_id,
        user_id=_user_id_for_task(db, task),
        gpu_seconds=totals.gpu_seconds,
        cpu_core_seconds=totals.cpu_core_seconds,
        ram_gb_seconds=totals.ram_gb_seconds,
        vram_gb_seconds=totals.vram_gb_seconds,
        disk_read_mb=totals.disk_read_mb,
        disk_write_mb=totals.disk_write_mb,
        wall_clock_seconds=totals.wall_clock_seconds,
        started_at=task.started_at,
        completed_at=task.completed_at,
    )
    db.add(agg)
    try:
        db.flush()
    except IntegrityError:
        # Concurrent insert of the same task_id — return the winner.
        db.rollback()
        winner = db.query(models.TaskResourceAggregate).filter(
            models.TaskResourceAggregate.task_id == task_id
        ).one_or_none()
        return winner, False
    return agg, True


_TERMINAL_STATES = (
    models.ApiTaskStatus.COMPLETED,
    models.ApiTaskStatus.FAILED,
    models.ApiTaskStatus.EXPIRED,
    models.ApiTaskStatus.CANCELLED,
)


def aggregate_pending(
    db: Session,
    since: datetime,
    until: datetime,
    *,
    batch_commit_every: int = 50,
) -> int:
    """Iterate terminal tasks completed in [since, until), aggregate each.

    Returns the number of NEW aggregate rows written. Tasks that already
    have an aggregate are skipped (idempotent re-run).
    """
    if since > until:
        return 0

    q = (
        db.query(models.ApiTaskQueue.id)
        .outerjoin(
            models.TaskResourceAggregate,
            models.TaskResourceAggregate.task_id == models.ApiTaskQueue.id,
        )
        .filter(
            models.ApiTaskQueue.status.in_(_TERMINAL_STATES),
            models.ApiTaskQueue.completed_at.isnot(None),
            models.ApiTaskQueue.completed_at >= since,
            models.ApiTaskQueue.completed_at < until,
            models.TaskResourceAggregate.id.is_(None),
        )
        .order_by(models.ApiTaskQueue.id.asc())
    )

    task_ids = [tid for (tid,) in q.all()]
    written = 0
    pending_in_tx = 0
    for task_id in task_ids:
        _row, was_new = aggregate_task(db, task_id)
        if was_new:
            written += 1
            pending_in_tx += 1
        if pending_in_tx >= batch_commit_every:
            db.commit()
            pending_in_tx = 0
    if pending_in_tx:
        db.commit()
    return written


# =============================================================================
# Leak-detection scan
# =============================================================================


@dataclass(frozen=True)
class _Thresholds:
    gpu: float
    cpu: float
    ram: float
    io: float

    @classmethod
    def from_config(cls, config: models.ResourceMonitorConfig) -> "_Thresholds":
        return cls(
            gpu=config.gpu_seconds_per_credit_threshold,
            cpu=config.cpu_core_seconds_per_credit_threshold,
            ram=config.ram_gb_seconds_per_credit_threshold,
            io=config.io_mb_per_credit_threshold,
        )

    def with_overrides(self, overrides: dict) -> "_Thresholds":
        """Apply a TenantThresholdOverride-shaped dict (any subset of
        the four keys). Omitted entries fall back to the platform
        default."""
        def _f(key, default):
            v = overrides.get(key)
            try:
                return float(v) if v is not None and float(v) > 0 else default
            except (TypeError, ValueError):
                return default
        return _Thresholds(
            gpu=_f("gpu_seconds_per_credit_threshold", self.gpu),
            cpu=_f("cpu_core_seconds_per_credit_threshold", self.cpu),
            ram=_f("ram_gb_seconds_per_credit_threshold", self.ram),
            io=_f("io_mb_per_credit_threshold", self.io),
        )


@dataclass(frozen=True)
class LeakOffender:
    tenant_id: int
    tenant_name: Optional[str]
    user_id: Optional[int]
    gpu_seconds: float
    cpu_core_seconds: float
    ram_gb_seconds: float
    io_mb: float
    credits_used: int
    # Per-resource ratios actually observed.
    gpu_seconds_per_credit: float
    cpu_core_seconds_per_credit: float
    ram_gb_seconds_per_credit: float
    io_mb_per_credit: float
    # Which resource(s) crossed their threshold this run.
    resources_above_threshold: tuple[str, ...] = ()
    task_count: int = 0


def _tenant_threshold_overrides(db: Session, tenant_id: int) -> dict:
    """Return the per-tenant JSONB override (or {}). Backwards-compat:
    a legacy float value for ``resource_leak_threshold`` (from the
    pre-de-dollarization draft) is ignored — operators must re-author
    the override in the new per-resource shape."""
    row = db.query(models.TenantSetting).filter(
        models.TenantSetting.tenant_id == tenant_id,
        models.TenantSetting.key == "resource_leak_threshold",
    ).one_or_none()
    if row is None:
        return {}
    v = row.value
    if isinstance(v, dict):
        return v
    return {}


def _top_tasks_for_offender(
    db: Session,
    tenant_id: int,
    user_id: Optional[int],
    since: datetime,
    until: datetime,
    limit: int = 5,
) -> list[dict]:
    """Top contributing aggregate rows for (tenant_id, user_id) in
    window, sorted by GPU-seconds desc as a stable hot-resource proxy.
    """
    q = db.query(models.TaskResourceAggregate).filter(
        models.TaskResourceAggregate.tenant_id == tenant_id,
        models.TaskResourceAggregate.completed_at >= since,
        models.TaskResourceAggregate.completed_at < until,
    )
    if user_id is None:
        q = q.filter(models.TaskResourceAggregate.user_id.is_(None))
    else:
        q = q.filter(models.TaskResourceAggregate.user_id == user_id)
    q = q.order_by(
        (models.TaskResourceAggregate.gpu_seconds
         + models.TaskResourceAggregate.cpu_core_seconds).desc()
    ).limit(limit)

    out = []
    for row in q.all():
        task = db.query(models.ApiTaskQueue).filter(
            models.ApiTaskQueue.id == row.task_id
        ).one_or_none()
        out.append({
            "task_id": row.task_id,
            "node_id": task.node_id if task else None,
            "workflow_run_id": task.workflow_run_id if task else None,
            "gpu_seconds": row.gpu_seconds,
            "cpu_core_seconds": row.cpu_core_seconds,
            "ram_gb_seconds": row.ram_gb_seconds,
            "io_mb": row.disk_read_mb + row.disk_write_mb,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        })
    return out


def _ratio(numerator: float, credits: int) -> float:
    """Native-unit-per-credit ratio. Zero credits → +∞ sentinel
    (1e9) so the UI distinguishes "ran with zero credits" from
    a finite extreme ratio."""
    if credits <= 0:
        return 1e9 if numerator > 0 else 0.0
    return numerator / credits


def scan_leaks(
    db: Session,
    *,
    window_seconds: int = 86400,
    now: Optional[datetime] = None,
) -> list[LeakOffender]:
    """Find (tenant_id, user_id) pairs above any per-resource
    threshold and emit one audit event per offender per window.

    Returns the list of offenders for whom an audit event was written
    this run (excluding any that failed the 24h debounce).
    """
    from sqlalchemy import func as sql_func

    from audit import has_recent_leak_event, log_resource_leak_detected

    # Guard: CE has no CreditUsageLog model — skip the credit-backed
    # attribution entirely rather than raising.  Keys on model
    # presence only: no licence gate and no quantity cap.
    if getattr(models, "CreditUsageLog", None) is None:
        log.info("resource_monitor: credit-attribution tables absent (CE); skipped")
        return []

    config = _get_config(db)
    if not config.enabled:
        log.info("resource_monitor: scan_leaks skipped (config.enabled=false)")
        return []

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(seconds=window_seconds)
    platform = _Thresholds.from_config(config)

    # Per (tenant, user) native sums.
    resource_q = (
        db.query(
            models.TaskResourceAggregate.tenant_id,
            models.TaskResourceAggregate.user_id,
            sql_func.sum(models.TaskResourceAggregate.gpu_seconds).label("gpu_seconds"),
            sql_func.sum(models.TaskResourceAggregate.cpu_core_seconds).label("cpu_core_seconds"),
            sql_func.sum(models.TaskResourceAggregate.ram_gb_seconds).label("ram_gb_seconds"),
            sql_func.sum(
                models.TaskResourceAggregate.disk_read_mb
                + models.TaskResourceAggregate.disk_write_mb
            ).label("io_mb"),
            sql_func.count(models.TaskResourceAggregate.id).label("task_count"),
        )
        .filter(
            models.TaskResourceAggregate.completed_at >= since,
            models.TaskResourceAggregate.completed_at < now,
        )
        .group_by(
            models.TaskResourceAggregate.tenant_id,
            models.TaskResourceAggregate.user_id,
        )
    )
    pairs: list[dict] = []
    tenant_ids: set[int] = set()
    for row in resource_q.all():
        pairs.append({
            "tenant_id": row.tenant_id,
            "user_id": row.user_id,
            "gpu_seconds": float(row.gpu_seconds or 0.0),
            "cpu_core_seconds": float(row.cpu_core_seconds or 0.0),
            "ram_gb_seconds": float(row.ram_gb_seconds or 0.0),
            "io_mb": float(row.io_mb or 0.0),
            "task_count": int(row.task_count or 0),
        })
        tenant_ids.add(row.tenant_id)

    # Per-tenant credit totals — same conservative attribution as
    # before: tenant credit divided across every offender inside that
    # tenant. Splitting by user requires a workflow_run join we don't
    # need today.
    tenant_credits: dict[int, int] = {}
    if tenant_ids:
        credit_q = (
            db.query(
                models.CreditUsageLog.tenant_id,
                sql_func.sum(models.CreditUsageLog.credits_used).label("credits_used"),
            )
            .filter(
                models.CreditUsageLog.tenant_id.in_(tenant_ids),
                models.CreditUsageLog.created_at >= since,
                models.CreditUsageLog.created_at < now,
            )
            .group_by(models.CreditUsageLog.tenant_id)
        )
        for row in credit_q.all():
            tenant_credits[row.tenant_id] = int(row.credits_used or 0)

    name_by_tenant: dict[int, str] = {}
    if tenant_ids:
        for t in db.query(models.Tenant).filter(models.Tenant.id.in_(tenant_ids)).all():
            name_by_tenant[t.id] = t.name

    emitted: list[LeakOffender] = []
    for agg in pairs:
        tenant_id = agg["tenant_id"]
        user_id = agg["user_id"]
        credits = tenant_credits.get(tenant_id, 0)

        # Resolve thresholds: per-tenant override falls back to platform.
        overrides = _tenant_threshold_overrides(db, tenant_id)
        thresholds = platform.with_overrides(overrides) if overrides else platform

        ratios = {
            "gpu": _ratio(agg["gpu_seconds"], credits),
            "cpu": _ratio(agg["cpu_core_seconds"], credits),
            "ram": _ratio(agg["ram_gb_seconds"], credits),
            "io":  _ratio(agg["io_mb"], credits),
        }

        above = tuple(
            kind for kind in RESOURCE_KINDS
            if ratios[kind] > getattr(thresholds, kind)
        )
        if not above:
            continue

        # min_alert_credits floor: when credits in window are below the
        # floor, suppress unless one resource is finite-and-high (i.e.
        # not the +∞ sentinel from divide-by-zero). Otherwise every
        # zero-credit user with any activity would alert.
        if credits < config.min_alert_credits:
            finite_above = [
                kind for kind in above if ratios[kind] < 1e9 / 2
            ]
            if not finite_above:
                continue

        if has_recent_leak_event(db, tenant_id, user_id, window_seconds):
            continue

        top_tasks = _top_tasks_for_offender(db, tenant_id, user_id, since, now)
        log_resource_leak_detected(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            ratios=ratios,
            thresholds={
                "gpu": thresholds.gpu, "cpu": thresholds.cpu,
                "ram": thresholds.ram, "io":  thresholds.io,
            },
            resources_above_threshold=above,
            native_totals={
                "gpu_seconds": agg["gpu_seconds"],
                "cpu_core_seconds": agg["cpu_core_seconds"],
                "ram_gb_seconds": agg["ram_gb_seconds"],
                "io_mb": agg["io_mb"],
            },
            credits_used=credits,
            top_tasks=top_tasks,
            window_seconds=window_seconds,
        )
        db.commit()

        emitted.append(LeakOffender(
            tenant_id=tenant_id,
            tenant_name=name_by_tenant.get(tenant_id),
            user_id=user_id,
            gpu_seconds=agg["gpu_seconds"],
            cpu_core_seconds=agg["cpu_core_seconds"],
            ram_gb_seconds=agg["ram_gb_seconds"],
            io_mb=agg["io_mb"],
            credits_used=credits,
            gpu_seconds_per_credit=ratios["gpu"],
            cpu_core_seconds_per_credit=ratios["cpu"],
            ram_gb_seconds_per_credit=ratios["ram"],
            io_mb_per_credit=ratios["io"],
            resources_above_threshold=above,
            task_count=agg["task_count"],
        ))

    return emitted


# =============================================================================
# Window helpers shared with the leak scan + admin endpoints
# =============================================================================


_WINDOW_SECONDS = {
    "1h":  3600,
    "24h": 86400,
    "7d":  86400 * 7,
    "30d": 86400 * 30,
}


def window_seconds_for(token: str) -> int:
    return _WINDOW_SECONDS.get(token, 86400)
