"""Capacity-floor-aware calibration dispatch for stale HardwareProfiles.

Bucket E — when a HardwareProfile is freshly detected OR flipped to
calibration_status='stale', queue the calibration matrix for it without
collapsing fleet headroom. Deferred when the fleet is busy; the daily
APScheduler sweep retries on the next round.

Single source of truth for "should I run calibration right now?":

    capacity_floor_pct (default 30): minimum fleet headroom that must
                                     remain after we queue calibration.
                                     0 = pack to 100%, 50 = leave half free.

Production callers do NOT block on this — failures are logged at WARNING
and the next sweep retries. Tests inject db_session + capacity overrides.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import or_

logger = logging.getLogger(__name__)

# Fleet-sizing "unlimited" sentinel.  The codebase spells numeric
# "effectively unlimited" as 1 quadrillion (``utils/credit_manager.py``
# ``UNLIMITED_SENTINEL``, ``utils/entitlements.py`` ``UNLIMITED``,
# ``schemas.PlanCreate``), and every concurrency reader already treats
# ``max_concurrent_tasks == 0`` as unlimited (``models.py`` column comment,
# ``routers/worker_tasks.py`` claim guard ``if worker.max_concurrent_tasks > 0``,
# the Go worker's ``maxConc <= 0`` gate).  A worker seeded 0 (the CE enrolment
# seed) therefore contributes this many calibration slots instead of 1, so the
# background-fleet cap never defers on it.  ``None``/missing keeps the
# historical 1-slot default, and Paid seeds 1, so Paid sizing is unchanged.
UNLIMITED_SLOTS = 1_000_000_000_000_000


def _unsupported_kinds_subq(db: Session):
    """Return a subquery of node_kinds flagged unsupported in the catalogue.

    Mirrors the guard in crud.node_definition_cud: system nodes whose
    custom_metadata['catalogue_status'] == 'unsupported'.  Callers should
    filter them out of any calibration-eligible set so an unsupported node
    is NEVER queued.
    """
    import models

    return db.query(models.NodeDefinition.node_kind).filter(
        models.NodeDefinition.tenant_id.is_(None),
        models.NodeDefinition.deleted_at.is_(None),
        models.NodeDefinition.node_kind.isnot(None),
        models.NodeDefinition.custom_metadata['catalogue_status'].as_string() == 'unsupported',
    ).subquery()


def _filter_out_unsupported(db: Session, query) -> "sqlalchemy.orm.Query":  # type: ignore  # noqa: F821
    """Add the unsupported-kinds exclusion to an existing NodeDefinition query.

    Usage:  query = _filter_out_unsupported(db, db.query(models.NodeDefinition).filter(...))
    """
    import models

    unsupported_kinds_subq = _unsupported_kinds_subq(db)
    return query.filter(or_(
        models.NodeDefinition.node_kind.is_(None),
        models.NodeDefinition.node_kind.notin_(unsupported_kinds_subq.select()),
    ))


def fleet_capacity_slots(db: Session) -> int:
    """Total task slots across active (LICENSED, non-paused) workers — each
    worker contributes ``max_concurrent_tasks`` slots (>=1; 0 = unlimited, the
    CE enrolment seed, which contributes the ``UNLIMITED_SLOTS`` sentinel so the
    shared background cap never defers on it). Used to size the shared
    background cap (calibration + variant-build) as a % of the fleet."""
    import models

    workers = db.query(models.WorkerLicense).filter(
        models.WorkerLicense.status == models.WorkerStatus.LICENSED,
        models.WorkerLicense.paused == False,  # noqa: E712
        models.WorkerLicense.deleted_at.is_(None),
    ).all()
    total = 0
    for w in workers:
        mct = w.max_concurrent_tasks
        if mct is None:
            total += 1  # unset keeps the historical 1-slot default
        elif mct == 0:
            total += UNLIMITED_SLOTS  # 0 = unlimited (CE enrolment seed)
        elif mct > 0:
            total += mct
        else:
            total += 1  # a nonsense negative keeps today's clamp
    return total


def pick_worker_for_profile(db: Session, hardware_profile_id: int) -> Optional[str]:
    """Pick an active (LICENSED, non-paused) worker on ``hardware_profile_id``
    for a pair-scoped calibration pin — the one with the highest
    ``max_concurrent_tasks``. Returns its ``worker_name`` or None."""
    import models

    worker = db.query(models.WorkerLicense).filter(
        models.WorkerLicense.status == models.WorkerStatus.LICENSED,
        models.WorkerLicense.paused == False,  # noqa: E712
        models.WorkerLicense.deleted_at.is_(None),
        models.WorkerLicense.hardware_profile_id == hardware_profile_id,
    ).order_by(
        models.WorkerLicense.max_concurrent_tasks.desc(),
        models.WorkerLicense.worker_name.asc(),
    ).first()
    return worker.worker_name if worker else None


def fleet_headroom_pct(db: Session) -> float:
    """Estimate current fleet headroom — what fraction of worker slots are free.

    Coarse heuristic: count active workers (LICENSED, not paused/draining)
    and the number of tasks currently RUNNING/CLAIMED. Each worker counts
    for `max_concurrent_tasks` slots (default 1 if unset). Headroom =
    (total_slots - active_tasks) / total_slots.

    Returns a value in [0, 1]. 1.0 = fully idle; 0.0 = fully booked.
    """
    import models

    workers = db.query(models.WorkerLicense).filter(
        models.WorkerLicense.status == models.WorkerStatus.LICENSED,
        models.WorkerLicense.paused == False,  # noqa: E712
        models.WorkerLicense.deleted_at.is_(None),
    ).all()
    if not workers:
        return 1.0

    total_slots = 0
    for w in workers:
        mct = w.max_concurrent_tasks
        if mct is None:
            total_slots += 1  # unset keeps the historical 1-slot default
        elif mct == 0:
            total_slots += UNLIMITED_SLOTS  # 0 = unlimited (CE enrolment seed)
        elif mct > 0:
            total_slots += mct
        else:
            total_slots += 1  # a nonsense negative keeps today's clamp

    if total_slots <= 0:
        return 1.0

    active = db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.status.in_([
            models.ApiTaskStatus.RUNNING, models.ApiTaskStatus.CLAIMED,
            models.ApiTaskStatus.ASSIGNED,
        ])
    ).count()

    free = max(0, total_slots - active)
    return free / total_slots


def should_dispatch_calibration(db: Session, capacity_floor_pct: Optional[int] = None) -> Tuple[bool, str]:
    """Decide whether to queue calibration tasks right now.

    Returns (allow, reason). reason is a short string the caller logs.
    """
    if capacity_floor_pct is None:
        # scheduler-runtime-config-panel — read from the runtime cache
        # which falls back through env var -> in-code default. Caller
        # may still override via the explicit kwarg (used by tests +
        # internal helpers).
        from services import scheduler_runtime_cache
        capacity_floor_pct = scheduler_runtime_cache.get_capacity_floor_pct()
    floor = capacity_floor_pct / 100.0

    headroom = fleet_headroom_pct(db)
    if headroom < floor:
        return (False, f"fleet headroom {headroom:.0%} < floor {floor:.0%}")
    return (True, f"fleet headroom {headroom:.0%} >= floor {floor:.0%}")


def auto_queue_for_profile(db: Session, profile_id: int, tenant_id: int = None,
                           capacity_floor_pct: Optional[int] = None,
                           runner=None) -> Optional[dict]:
    """A new / changed HardwareProfile means every node may need recalibration
    (and a variant build) for that hardware.

    The OLD path fired ``batch_calibrator.run_batch_calibration`` — enqueuing
    all ~42k calibration tasks at once. With a flaky worker re-enrolling in a
    loop (e.g. a false thermal-throttle), that re-fired every few minutes and
    buried the fleet under tens of thousands of tasks.

    Now: mark eligible nodes ``stale`` and let the BOUNDED calibration
    coordinator drain them under the shared background-fleet cap. Variant
    builds for the new accelerator are handled (also bounded) by the variant
    backfill sweep. Throttling is owned end-to-end by the coordinator, so the
    old capacity-floor gate no longer applies here.

    ``runner`` is kept for test injection; when provided it is honored verbatim.
    """
    if runner is not None:
        try:
            return runner()
        except Exception as e:  # noqa: BLE001
            logger.warning("auto_queue_for_profile=%s runner failed: %s", profile_id, e)
            return None

    import models
    # Platform tenant owns the system calibration matrix.
    tid = tenant_id or 1
    try:
        marked = _filter_out_unsupported(
            db,
            db.query(models.NodeDefinition).filter(
                models.NodeDefinition.node_kind.isnot(None),
                models.NodeDefinition.executable_path.isnot(None),
                models.NodeDefinition.excluded_from_calibration.is_(False),
                models.NodeDefinition.calibration_status != "stale",
            ),
        ).update(
            {models.NodeDefinition.calibration_status: "stale"},
            synchronize_session=False,
        )
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("auto_queue_for_profile=%s mark-stale failed: %s", profile_id, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None

    # Ensure a running coordinator plan exists so the bounded tick drains
    # the freshly-staled nodes.
    try:
        from services.calibration_coordinator import ensure_plan
        ensure_plan(db, tenant_id=tid)
    except Exception as e:  # noqa: BLE001
        logger.warning("auto_queue_for_profile=%s ensure_plan failed: %s", profile_id, e)

    logger.info(
        "auto_queue_for_profile=%s marked %d node(s) stale; bounded coordinator "
        "will recalibrate under the background cap (variant-builds via backfill)",
        profile_id, marked,
    )
    return {"marked_stale": marked, "mode": "coordinator-bounded"}


def auto_queue_for_node_profile_pair(db: Session, *, node_definition_id: int,
                                      hardware_profile_id: int, tenant_id: int = None,
                                      trigger_source: str = 'drift_scan',
                                      capacity_floor_pct: Optional[int] = None) -> Optional[dict]:
    """Queue a pair-scoped calibration matrix for one node definition against
    one hardware profile.

    This is the "drift scan" dispatcher: when prediction MAE exceeds a threshold
    for a node-on-profile pair, we enqueue just the isolation runs (not the full
    84-task matrix) to get fresh calibration data without overwhelming the fleet.

    Returns None if:
      - should_dispatch_calibration denies (capacity floor)
      - the node definition is missing
      - no active worker is on the hardware profile

    Deliberately does NOT skip 'calibrated' nodes: a drift re-cal targets
    exactly the pairs that are already calibrated but whose predictions drifted.

    On success, returns:
      {"task_ids": [...], "worker_name": str, "mode": "pair-isolation"}

    Calibration tasks are never hardware-pinned by default, so the pair is
    pinned via ``assigned_to_worker`` + ASSIGNED. ``_enqueue_calibration_task``
    only flushes; this function commits once after every tier is built and
    rolls back on any failure, so a pair is queued completely or not at all.
    """
    import models
    from routers.node_definitions_domains._shared import (
        CALIBRATION_RUNS_PER_TIER,
        SIZE_TIERS,
        _enqueue_calibration_task,
    )

    tid = tenant_id or 1

    node_def = _filter_out_unsupported(
        db,
        db.query(models.NodeDefinition).filter(
            models.NodeDefinition.id == node_definition_id,
        ),
    ).first()
    if not node_def:
        logger.warning(
            "auto_queue_for_node_profile_pair node=%s profile=%s: node not found",
            node_definition_id, hardware_profile_id,
        )
        return None

    allow, reason = should_dispatch_calibration(db, capacity_floor_pct=capacity_floor_pct)
    if not allow:
        logger.info(
            "auto_queue_for_node_profile_pair node=%s profile=%s deferred: %s",
            node_definition_id, hardware_profile_id, reason,
        )
        return None

    worker_name = pick_worker_for_profile(db, hardware_profile_id)
    if not worker_name:
        logger.warning(
            "auto_queue_for_node_profile_pair node=%s profile=%s: no active worker on profile",
            node_definition_id, hardware_profile_id,
        )
        return None

    task_ids: List[int] = []
    try:
        for tier in SIZE_TIERS:
            for run in range(CALIBRATION_RUNS_PER_TIER):
                task = _enqueue_calibration_task(
                    db, node_def, tid, tier=tier, run=run, run_kind="isolation",
                )
                task.assigned_to_worker = worker_name
                task.status = models.ApiTaskStatus.ASSIGNED
                task.task_payload = {
                    **(task.task_payload or {}),
                    "hardware_profile_id": hardware_profile_id,
                    "trigger_source": trigger_source,
                }
                task_ids.append(task.id)
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "auto_queue_for_node_profile_pair node=%s profile=%s enqueue failed: %s",
            node_definition_id, hardware_profile_id, e,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None

    logger.info(
        "auto_queue_for_node_profile_pair node=%s profile=%s worker=%s: queued %d tasks",
        node_definition_id, hardware_profile_id, worker_name, len(task_ids),
    )

    return {
        "task_ids": task_ids,
        "worker_name": worker_name,
        "mode": "pair-isolation",
    }


def auto_queue_for_node(db: Session, node_definition_id: int,
                        tenant_id_for_dispatch: int,
                        capacity_floor_pct: Optional[int] = None,
                        runner=None) -> Optional[dict]:
    """Best-effort calibration dispatch for one new NodeDefinition.

    Symmetric to auto_queue_for_profile in the orthogonal axis: that one
    calibrates *all nodes* against a freshly-registered hardware profile;
    this one calibrates *one node* against the existing fleet so a
    newly-added node has prediction data instead of falling back to
    heuristic.

    Skips when:
      - fleet headroom is below `CALIBRATION_CAPACITY_FLOOR_PCT`
        (returns `None`, daily staleness sweep retries on next round)
      - the node is missing or already in `calibrating` / `calibrated`
        state (returns `None`, INFO-logged)

    Returns a dispatch result dict on success.

    `runner` is an optional callable for test injection — production
    callers omit it; the default lazily imports
    `routers.node_definitions._dispatch_calibration_tasks`.
    """
    import models
    node_def = _filter_out_unsupported(
        db,
        db.query(models.NodeDefinition).filter(
            models.NodeDefinition.id == node_definition_id,
        ),
    ).first()
    if not node_def:
        logger.warning(
            "auto_queue_for_node=%s skipped: node definition not found",
            node_definition_id,
        )
        return None

    if node_def.calibration_status in ("calibrating", "calibrated"):
        logger.info(
            "auto_queue_for_node=%s skipped: status=%s",
            node_definition_id, node_def.calibration_status,
        )
        return None

    if runner is not None:
        try:
            return runner()
        except Exception as e:  # noqa: BLE001
            logger.warning("auto_queue_for_node=%s runner failed: %s", node_definition_id, e)
            return None

    # Don't dispatch the 84-task matrix directly (that bypasses the shared
    # background cap). A freshly-registered node is 'uncalibrated', which makes
    # it eligible for the BOUNDED coordinator; just make sure it's flagged and a
    # plan is running so the coordinator drains it under the fleet-% cap.
    if node_def.calibration_status not in ("uncalibrated", "stale"):
        node_def.calibration_status = "stale"
        db.commit()
    try:
        from services.calibration_coordinator import ensure_plan
        ensure_plan(db, tenant_id=tenant_id_for_dispatch or 1)
    except Exception as e:  # noqa: BLE001
        logger.warning("auto_queue_for_node=%s ensure_plan failed: %s", node_definition_id, e)
    logger.info(
        "auto_queue_for_node=%s queued for bounded coordinator (status=%s)",
        node_definition_id, node_def.calibration_status,
    )
    return {"node_definition_id": node_definition_id, "mode": "coordinator-bounded"}
