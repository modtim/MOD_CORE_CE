"""Calibration coordinator — in-flight-bounded dispatch.

Replaces `batch_calibrator.run_batch_calibration`'s fire-and-forget
"enqueue all 42,672 tasks at once" model with a paced coordinator:

  every N seconds:
    if no plan running                    → no-op
    if plan paused/completed/cancelled    → no-op
    count in-flight calibration tasks (CLAIMED + RUNNING + ASSIGNED)
    if in-flight >= target_in_flight      → defer; record reason
    if user-priority backlog is high      → defer; record reason
    pick next eligible NodeDef            → enqueue its 84-task matrix
    sweep finished NodeDefs to 'calibrated'

The "user-priority backlog" gate keeps calibration from squeezing user
work even though calibration tasks already run at priority=2.

Failure modes the older dispatcher had:
- enqueueing 42k tasks immediately filled the queue past the 30%
  capacity floor, so `should_dispatch_calibration` would never re-fire
  to pull more eligible nodes (self-deadlock)
- workers without `max_concurrent_tasks` set greedily claimed hundreds
  of tasks at once, overwhelming the host's Docker daemon + systemd
  cgroup machinery
- no graceful pause/resume; cancellation meant manual queue purge

This module addresses both: in-flight is the bound, not queue depth;
state lives in `calibration_plans` so pause/resume/cancel are clean.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from utils.paid_import import optional_paid, is_ce_edition

logger = logging.getLogger(__name__)

# 2400CR1zf — the CE notice logs once per process (tick() runs every 30s)
_CE_LOGGED = False


# Tunables. Conservative defaults; tweak via plan row, env, or future
# admin endpoint. The coordinator picks NodeDefs in id order so the
# first one always gets the most attention — fine for now since order
# isn't load-bearing.
DEFAULT_TARGET_IN_FLIGHT = 30
USER_BACKLOG_PRIORITY_FLOOR = 5  # priority >= 5 = user/system work
USER_BACKLOG_DEFER_THRESHOLD = 20  # if >N user tasks queued, defer calibration


# --- Failure-rate dispatch brake -------------------------------------------
# The in-flight cap cannot perceive a total-failure mode: a task that fails in
# ~2s never accumulates as in-flight, so `_count_in_flight()` reads 0 on every
# tick and the cap never engages. Plan id=1 burned 2026-07-26 -> 2026-07-29 that
# way, dispatching 5,292 tasks at an 8% success rate. This brake bounds dispatch
# on observed FAILURE RATE instead, which a fast-failure mode cannot hide from.
#
# Deliberately not named circuit_breaker: the shipped `circuit-breaker-removal`
# capability requires that identifier to appear nowhere in active code.
#
# Read via functions rather than module constants so env changes (and tests)
# take effect without a reimport.

def _brake_window() -> int:
    """How many recent terminal calibration tasks the brake looks at."""
    return int(os.getenv("CALIBRATION_BRAKE_WINDOW", "50"))


def _brake_min_samples() -> int:
    """Floor below which the brake never fires, so a handful of unlucky
    tasks can't halt a healthy plan."""
    return int(os.getenv("CALIBRATION_BRAKE_MIN_SAMPLES", "20"))


def _brake_fail_pct() -> float:
    """Failed percentage at or above which dispatch halts."""
    return float(os.getenv("CALIBRATION_BRAKE_FAIL_PCT", "80"))


def _min_success_pct() -> float:
    """Successful percentage a node's matrix must reach to be recorded as
    calibrated. Zero successes never calibrates regardless of this value."""
    return float(os.getenv("CALIBRATION_MIN_SUCCESS_PCT", "50"))


def dispatch_brake(db: Session, plan) -> dict:
    """Evaluate the failure-rate brake for `plan`.

    Looks at the most recent terminal (COMPLETED/FAILED) calibration tasks
    created since the plan started. Returns a dict with `samples`,
    `failed`, `failed_pct` and `engaged`.

    Scoped to the whole plan rather than per-node on purpose: each node's
    matrix failed completely, was swept as 'done', and the plan advanced to
    the next node. Only a plan-wide view sees that *every* node is failing.
    """
    from models import ApiTaskQueue, ApiTaskStatus

    q = db.query(ApiTaskQueue.status).filter(
        ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
        ApiTaskQueue.status.in_((ApiTaskStatus.COMPLETED, ApiTaskStatus.FAILED)),
    )
    if getattr(plan, "created_at", None) is not None:
        q = q.filter(ApiTaskQueue.created_at >= plan.created_at)

    rows = q.order_by(ApiTaskQueue.id.desc()).limit(_brake_window()).all()

    samples = len(rows)
    failed = sum(1 for (s,) in rows if s == ApiTaskStatus.FAILED)
    failed_pct = (failed / samples * 100.0) if samples else 0.0
    engaged = samples >= _brake_min_samples() and failed_pct >= _brake_fail_pct()

    return {
        "samples": samples,
        "failed": failed,
        "failed_pct": failed_pct,
        "engaged": engaged,
    }


def _count_in_flight(db: Session) -> int:
    """SHARED background budget: how many background tasks are in-flight
    (CLAIMED + RUNNING + ASSIGNED) — calibration tasks PLUS variant-build
    backfill tasks. PENDING is not counted because the gate is 'don't put
    MORE on the docker daemon's plate', not 'don't enqueue'. Calibration and
    variant-builds share one budget so neither can starve real user work."""
    from models import ApiTaskQueue, ApiTaskStatus, WorkflowRun

    non_terminal = [
        ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING, ApiTaskStatus.ASSIGNED,
    ]
    calib = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status.in_(non_terminal),
        ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
    ).count()
    # Variant-build backfill tasks ride on a workflow_run flagged
    # variant_backfill (see node_builder/variant_test/backfill.py); they
    # count against the same background budget.
    try:
        backfill = db.query(ApiTaskQueue).join(
            WorkflowRun, WorkflowRun.id == ApiTaskQueue.workflow_run_id,
        ).filter(
            ApiTaskQueue.status.in_(non_terminal),
            WorkflowRun.parameters["project_info"]["extra_variables"][
                "variant_backfill"].as_string() == "true",
        ).count()
    except Exception:  # noqa: BLE001 — never let the count crash the tick
        backfill = 0
    return calib + backfill


def background_cap(db: Session) -> int:
    """The shared in-flight cap for background work, as a live % of the fleet
    (the scheduler 'Background work uses at most X% of the fleet' slider).
    Always >= 1 so a single-slot fleet can still make slow progress."""
    from services.calibration_dispatcher import fleet_capacity_slots
    from services import scheduler_runtime_cache
    slots = fleet_capacity_slots(db)
    pct = scheduler_runtime_cache.get_background_max_fleet_pct()
    return max(1, (slots * pct) // 100)


def _count_user_backlog(db: Session) -> int:
    """How many high-priority user/system tasks are sitting PENDING.
    If this is non-trivial, calibration defers entirely until the
    backlog drains, even if our own in-flight cap allows more."""
    from models import ApiTaskQueue, ApiTaskStatus

    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status == ApiTaskStatus.PENDING,
        ApiTaskQueue.priority >= USER_BACKLOG_PRIORITY_FLOOR,
    ).count()


def _count_eligible_nodes(db: Session) -> int:
    """NodeDefs that need calibration (`uncalibrated` or `stale`),
    have a dispatchable image (`node_kind` + `executable_path`), and
    aren't already mid-flight (status='calibrating'). Used to size
    the plan at creation time and to detect completion.

    Rows with ``excluded_from_calibration=true`` (system one-shots,
    GPU-only, external-endpoint, Vault-dependent nodes — see the
    20260514 migration) are filtered out: the calibration matrix has
    no useful signal to extract from them and they were producing
    nodes_failed=N pollution every plan.
    """
    from models import NodeDefinition

    return db.query(NodeDefinition).filter(
        NodeDefinition.node_kind.isnot(None),
        NodeDefinition.executable_path.isnot(None),
        NodeDefinition.calibration_status.in_(("uncalibrated", "stale")),
        NodeDefinition.excluded_from_calibration.is_(False),
    ).count()


def _pick_next_node(db: Session, exclude_ids: List[int]):
    """Next NodeDef whose calibration_status is uncalibrated/stale and
    is not currently in nodes_in_progress. id-ordered so the first call
    is deterministic and tests are stable. Excludes rows flagged
    ``excluded_from_calibration`` (see _count_eligible_nodes docstring).
    """
    from models import NodeDefinition

    q = db.query(NodeDefinition).filter(
        NodeDefinition.node_kind.isnot(None),
        NodeDefinition.executable_path.isnot(None),
        NodeDefinition.calibration_status.in_(("uncalibrated", "stale")),
        NodeDefinition.excluded_from_calibration.is_(False),
    )
    if exclude_ids:
        q = q.filter(~NodeDefinition.id.in_(exclude_ids))
    return q.order_by(NodeDefinition.id.asc()).first()


def _fixture_gate_active() -> bool:
    """GC.PFM.10 gate cutover flag (PFM.19).

    The fixture gate is inert unless the fixture_shared_bucket_enabled
    platform setting (env fallback MOD_FIXTURE_SHARED_BUCKET, value '1') is
    on: until cutover the migrator image isn't in Harbor and no tenant reaches
    'ready', so gating by default would 409 calibration for every
    pending/NULL-tenant. Mirrors the users_router_service.create_tenant
    provisioning-hook gate."""
    from services import platform_settings as _psr
    return bool(_psr.get_setting("fixture_shared_bucket_enabled"))


def _ce_autoqueue_disabled() -> bool:
    """2400CR1zf — calibration must not auto-start on Community Edition.

    The matrix's generator image (ghcr.io/modtim/calibrationinputgenerator)
    isn't shipped in the CE carve, so every calgen task fails `denied` at the
    pull. One upstream gate, reused by both entry points on the auto-queue
    path: `ensure_plan` (reached from calibration_dispatcher's
    auto_queue_for_node/auto_queue_for_profile) and `tick()` (the
    scheduler's calibration_coordinator_tick job, which would otherwise
    dispatch root tasks for plan rows that already exist). Edition is the
    canonical predicate from utils/paid_import.py:56 — no new tunable.
    Existing plan rows are left alone; nothing is deleted."""
    global _CE_LOGGED
    if not is_ce_edition():
        return False
    if not _CE_LOGGED:
        _CE_LOGGED = True
        logger.info("calibration auto-queue disabled on Community Edition")
    return True


def create_plan(db: Session, tenant_id: int,
                target_in_flight: int = DEFAULT_TARGET_IN_FLIGHT) -> "CalibrationPlan":
    """Initialize a new running plan for `tenant_id`. Fails if one
    is already running for this tenant (DB partial-unique enforces).
    Snapshots `total_nodes` so progress is computable. Raises
    ValueError when the tenant's calibration fixtures are not ready
    (GC.PFM.10) — only once the PFM.19 cutover flag is on."""
    from models import CalibrationPlan

    if _fixture_gate_active():
        from services.fixture_service import fixtures_ready

        if not fixtures_ready(db, tenant_id):
            raise ValueError("calibration fixtures not ready")

    existing = db.query(CalibrationPlan).filter(
        CalibrationPlan.tenant_id == tenant_id,
        CalibrationPlan.status == "running",
    ).first()
    if existing is not None:
        raise ValueError(
            f"calibration plan {existing.id} is already running for tenant {tenant_id}"
        )

    total = _count_eligible_nodes(db)
    plan = CalibrationPlan(
        tenant_id=tenant_id,
        status="running",
        target_in_flight=int(target_in_flight),
        total_nodes=int(total),
        nodes_completed=0,
        nodes_failed=0,
        nodes_in_progress=[],
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    logger.info(
        "calibration plan %d created (tenant=%s total_nodes=%d target_in_flight=%d)",
        plan.id, tenant_id, total, target_in_flight,
    )
    return plan


def ensure_plan(db: Session, tenant_id: int,
                target_in_flight: int = DEFAULT_TARGET_IN_FLIGHT) -> "CalibrationPlan":
    """Return the running plan for `tenant_id`, creating one if none exists.
    Idempotent — safe to call on every profile change / staleness sweep so
    the bounded coordinator has a plan to drain newly-staled nodes through."""
    from models import CalibrationPlan
    existing = db.query(CalibrationPlan).filter(
        CalibrationPlan.tenant_id == tenant_id,
        CalibrationPlan.status == "running",
    ).first()
    if existing is not None:
        return existing
    if _ce_autoqueue_disabled():
        # 2400CR1zf — CE: never start a plan (the generator image isn't in
        # the CE carve, so every calgen task would fail `denied`).
        return None
    if _fixture_gate_active():
        from services.fixture_service import fixtures_ready

        if not fixtures_ready(db, tenant_id):
            # GC.PFM.10 — gate: never start a plan for a tenant whose
            # calibration fixtures are not ready. Callers ignore the
            # None return (calibration_dispatcher L174/L345).
            logger.debug(
                "ensure_plan: skipping plan for tenant %s — calibration fixtures not ready",
                tenant_id,
            )
            return None
    return create_plan(db, tenant_id, target_in_flight)


def _release_in_progress_nodes(db: Session, plan) -> int:
    """Return nodes listed in `plan.nodes_in_progress` to 'uncalibrated' so a
    future plan can pick them up again. Only touches rows still sitting in
    'calibrating' — a node that already reached a terminal status is left
    alone."""
    from models import NodeDefinition

    in_progress = list(plan.nodes_in_progress or [])
    if not in_progress:
        return 0

    return db.query(NodeDefinition).filter(
        NodeDefinition.id.in_(in_progress),
        NodeDefinition.calibration_status == "calibrating",
    ).update({"calibration_status": "uncalibrated"}, synchronize_session=False)


def reclaim_stranded_nodes(db: Session) -> int:
    """Reconcile nodes stuck in 'calibrating' that have no non-terminal
    calibration tasks left.

    A node reaches this state when its plan was cancelled, or when tasks were
    purged out from under it. `_pick_next_node()` selects only
    uncalibrated/stale, so a stranded node is invisible to every future plan
    until something puts it back. Returns the number reclaimed."""
    from models import ApiTaskQueue, ApiTaskStatus, NodeDefinition

    stranded = db.query(NodeDefinition).filter(
        NodeDefinition.calibration_status == "calibrating",
    ).all()
    if not stranded:
        return 0

    terminal = (ApiTaskStatus.COMPLETED, ApiTaskStatus.FAILED, ApiTaskStatus.CANCELLED)
    reclaimed = 0

    for node in stranded:
        rows = db.query(ApiTaskQueue.status).filter(
            ApiTaskQueue.task_payload["node_definition_id"].as_string() == str(node.id),
            ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
        ).all()
        # No tasks at all, or every task terminal => nothing is coming.
        if any(s not in terminal for (s,) in rows):
            continue

        node.calibration_status = "uncalibrated"
        reclaimed += 1
        logger.info(
            "calibration: reclaimed stranded node %d (was 'calibrating' with "
            "no live tasks)", node.id,
        )

    return reclaimed


def cancel_plan(db: Session, plan_id: int) -> "CalibrationPlan":
    """Move plan to 'cancelled' AND mark every PENDING calibration
    task tied to it as CANCELLED so workers stop pulling them. Tasks
    already CLAIMED/RUNNING are left alone — they'll terminate
    naturally and the next sweep will reconcile."""
    from models import CalibrationPlan, ApiTaskQueue, ApiTaskStatus

    plan = db.query(CalibrationPlan).filter(CalibrationPlan.id == plan_id).first()
    if plan is None:
        raise ValueError(f"plan {plan_id} not found")
    if plan.status in ("cancelled", "completed"):
        return plan

    plan.status = "cancelled"

    # Best-effort purge of the pending queue. Use a server-side
    # UPDATE so we don't materialize tens of thousands of rows.
    cancelled = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status == ApiTaskStatus.PENDING,
        ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
    ).update({"status": ApiTaskStatus.CANCELLED}, synchronize_session=False)

    # Release nodes this plan had in flight. Without this they stay in
    # calibration_status='calibrating' forever, and _pick_next_node() only
    # selects uncalibrated/stale — so a cancelled plan silently strands its
    # in-progress nodes and excludes them from every future plan. 15 nodes
    # were stranded exactly this way.
    released = _release_in_progress_nodes(db, plan)
    plan.nodes_in_progress = []

    db.commit()
    db.refresh(plan)
    logger.warning(
        "calibration plan %d cancelled — purged %d pending calibration tasks, "
        "released %d in-progress node(s)",
        plan_id, cancelled, released,
    )
    return plan


def _sweep_completed_nodes(db: Session, plan) -> int:
    """For each NodeDef currently in `plan.nodes_in_progress`, count its
    queue rows. If all 84 are terminal (COMPLETED + FAILED + CANCELLED),
    flip the NodeDef to `calibration_status='calibrated'` and remove it
    from in_progress. Increments `plan.nodes_completed` for the swept
    nodes, plus `nodes_failed` if the per-node failure rate is high.

    Returns count of nodes swept this tick."""
    from models import ApiTaskQueue, ApiTaskStatus, NodeDefinition

    in_progress = list(plan.nodes_in_progress or [])
    if not in_progress:
        return 0

    swept = 0
    still_running: List[int] = []

    for node_id in in_progress:
        # 18.4 (GI.4): k8s per-node fan-out runs carry the same
        # calibration_run + node_definition_id markers but are NOT part of
        # the 84-task matrix — count only the matrix rows, so a stuck or
        # slow k8s pod can never hold the docker matrix's completion
        # hostage.
        k8s_ids = [r[0] for r in db.query(ApiTaskQueue.id).filter(
            ApiTaskQueue.task_payload["node_definition_id"].as_string() == str(node_id),
            ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
            ApiTaskQueue.task_payload["k8s_calibration_node"].as_string().isnot(None),
        ).all()]
        rq = db.query(ApiTaskQueue.status).filter(
            ApiTaskQueue.task_payload["node_definition_id"].as_string() == str(node_id),
            ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
        )
        if k8s_ids:
            rq = rq.filter(~ApiTaskQueue.id.in_(k8s_ids))
        rows = rq.all()
        if not rows:
            still_running.append(node_id)
            continue

        terminal = sum(
            1 for (s,) in rows
            if s in (ApiTaskStatus.COMPLETED, ApiTaskStatus.FAILED, ApiTaskStatus.CANCELLED)
        )
        if terminal != len(rows):
            still_running.append(node_id)
            continue

        # Honest status. FAILED is terminal for "is this node still running?"
        # but it is NOT evidence of a measurement. A node is only 'calibrated'
        # when its matrix actually produced successes.
        #
        # Before this rule, 41 node definitions were marked calibrated with
        # 0 successes out of 84 — and because _pick_next_node() only selects
        # uncalibrated/stale, they were then permanently excluded from every
        # future plan and could never self-heal.
        succeeded = sum(1 for (s,) in rows if s == ApiTaskStatus.COMPLETED)
        success_pct = (succeeded / len(rows) * 100.0) if rows else 0.0
        node_failed = succeeded == 0 or success_pct < _min_success_pct()

        node = db.query(NodeDefinition).filter(NodeDefinition.id == node_id).first()
        if node is not None:
            # 'failed' is terminal and NOT selected by _pick_next_node(), so a
            # broken node does not get retried forever. An operator resets it
            # to 'uncalibrated' to try again.
            node.calibration_status = "failed" if node_failed else "calibrated"
            if not node_failed:
                node.last_calibrated_at = datetime.now(timezone.utc)

        plan.nodes_completed = (plan.nodes_completed or 0) + 1
        if node_failed:
            plan.nodes_failed = (plan.nodes_failed or 0) + 1
            logger.warning(
                "calibration: node %d recorded FAILED — %d/%d succeeded (%.0f%%, "
                "floor %.0f%%)",
                node_id, succeeded, len(rows), success_pct, _min_success_pct(),
            )
        swept += 1

    plan.nodes_in_progress = still_running
    return swept


def tick(db: Session) -> Optional[dict]:
    """One coordinator step. Called by APScheduler every N seconds.

    Returns a small dict describing what happened (for tests / logs).
    Returns None when there is no running plan (cheap no-op path)."""
    from models import CalibrationPlan, NodeDefinition
    from sqlalchemy.orm.attributes import flag_modified

    if _ce_autoqueue_disabled():
        # 2400CR1zf — CE: the scheduler tick must not dispatch root tasks for
        # any plan row (pre-existing rows are left untouched, never deleted).
        return None

    plan = db.query(CalibrationPlan).filter(
        CalibrationPlan.status == "running",
    ).first()
    if plan is None:
        return None

    now = datetime.now(timezone.utc)

    # Reconcile completed-but-still-listed nodes first so we free up
    # in-flight headroom before the dispatch decision.
    swept = _sweep_completed_nodes(db, plan)

    # Return any node stranded in 'calibrating' with nothing live behind it,
    # so a cancelled or interrupted plan doesn't permanently hide nodes.
    reclaim_stranded_nodes(db)

    # Failure-rate brake. Checked BEFORE the in-flight cap because a
    # fast-failure mode drives in-flight to 0, which makes the cap read as
    # "plenty of headroom" exactly when the plan is doing nothing but fail.
    brake = dispatch_brake(db, plan)
    if brake["engaged"]:
        plan.status = "halted"
        plan.last_tick_at = now
        plan.last_tick_reason = (
            f"halted: {brake['failed']}/{brake['samples']} recent calibration "
            f"tasks failed ({brake['failed_pct']:.0f}% >= {_brake_fail_pct():.0f}%)"
        )
        flag_modified(plan, "nodes_in_progress")
        db.commit()
        logger.error(
            "calibration plan %d HALTED by failure brake — %s",
            plan.id, plan.last_tick_reason,
        )
        return {"action": "halted", "reason": plan.last_tick_reason,
                "swept": swept, "brake": brake}

    in_flight = _count_in_flight(db)
    user_backlog = _count_user_backlog(db)
    # Live cap from the 'Background work uses at most X% of the fleet' slider.
    cap = background_cap(db)

    if user_backlog >= USER_BACKLOG_DEFER_THRESHOLD:
        plan.last_tick_at = now
        plan.last_tick_reason = (
            f"deferred: user backlog {user_backlog} >= "
            f"{USER_BACKLOG_DEFER_THRESHOLD}"
        )
        flag_modified(plan, "nodes_in_progress")
        db.commit()
        return {"action": "deferred", "reason": plan.last_tick_reason, "swept": swept}

    if in_flight >= cap:
        plan.last_tick_at = now
        plan.last_tick_reason = (
            f"deferred: background in-flight {in_flight} >= cap {cap} "
            f"(fleet %)"
        )
        flag_modified(plan, "nodes_in_progress")
        db.commit()
        return {"action": "deferred", "reason": plan.last_tick_reason, "swept": swept}

    next_node = _pick_next_node(db, exclude_ids=list(plan.nodes_in_progress or []))
    if next_node is None:
        # Nothing left to dispatch. If in_flight has drained, plan is done.
        if in_flight == 0 and not plan.nodes_in_progress:
            plan.status = "completed"
            plan.last_tick_at = now
            plan.last_tick_reason = "completed: no eligible nodes left, in-flight drained"
            db.commit()
            return {"action": "completed", "swept": swept}
        plan.last_tick_at = now
        plan.last_tick_reason = "waiting: no eligible nodes; draining in-flight"
        flag_modified(plan, "nodes_in_progress")
        db.commit()
        return {"action": "waiting", "swept": swept}

    from services import calibration_workflow as cw

    # Preflight before touching the node's status. A node whose input format
    # we cannot generate must be recorded and skipped, not dispatched into a
    # certain failure — that is the class of failure that produced 3,612
    # failed tasks. 'failed' is terminal and not reselected by
    # _pick_next_node(), so the plan advances instead of looping on it.
    check = cw.preflight(next_node)
    if not check["ok"]:
        next_node.calibration_status = "failed"
        plan.nodes_failed = (plan.nodes_failed or 0) + 1
        plan.last_tick_at = now
        plan.last_tick_reason = f"skipped: {check['reason']}"
        flag_modified(plan, "nodes_in_progress")
        db.commit()
        logger.warning("calibration tick: %s", plan.last_tick_reason)
        return {"action": "skipped", "reason": plan.last_tick_reason, "swept": swept}

    worker_name = cw.pick_worker(db, next_node.process_type or "CPU")
    if worker_name is None:
        plan.last_tick_at = now
        plan.last_tick_reason = (
            f"deferred: no licensed heartbeating worker serving queue "
            f"{next_node.process_type or 'CPU'}"
        )
        flag_modified(plan, "nodes_in_progress")
        db.commit()
        return {"action": "deferred", "reason": plan.last_tick_reason, "swept": swept}

    # Mark NodeDef as 'calibrating' before dispatch so concurrent ticks don't
    # pick the same one.
    next_node.calibration_status = "calibrating"
    db.flush()

    # Bound this tick's dispatch to the remaining headroom rather than queuing
    # a whole matrix against a cap of `target_in_flight`. Runs are pinned
    # workflows now, not bare tasks: each is
    # [calibration_input_generator] -> [node under test] on one worker.
    headroom = max(0, cap - in_flight)
    runs = cw.dispatch_matrix(
        db, next_node, plan.tenant_id, worker_name, max_runs=headroom or None,
    )

    if not runs:
        # Nothing dispatched (no headroom, or preflight refused mid-way).
        next_node.calibration_status = "uncalibrated"
        plan.last_tick_at = now
        plan.last_tick_reason = (
            f"deferred: no headroom for node {next_node.id} "
            f"(in-flight {in_flight} of cap {cap})"
        )
        flag_modified(plan, "nodes_in_progress")
        db.commit()
        return {"action": "deferred", "reason": plan.last_tick_reason, "swept": swept}

    new_in_progress = list(plan.nodes_in_progress or [])
    new_in_progress.append(next_node.id)
    plan.nodes_in_progress = new_in_progress
    plan.last_tick_at = now
    plan.last_tick_reason = (
        f"dispatched node {next_node.id} ({next_node.name}) — "
        f"{len(runs)} pinned run(s) on {worker_name}; "
        f"in-flight {in_flight} -> ~{in_flight + len(runs)}"
    )
    flag_modified(plan, "nodes_in_progress")
    db.commit()

    task_ids = [r.id for r in runs]
    # k8s_task_count defaults to 0 when the paid service is absent
    # (CE / no k8s_calibration) or when the dispatch call raises —
    # prevents UnboundLocalError on the later f-string and log line.
    k8s_task_count = 0
    # 18.4 (GI.4): ride this tick's dispatch with the k8s per-node fan-out —
    # one pinned isolation run per k8s node across the plan tenant's active
    # clusters. It inherits everything the docker dispatch just passed: the
    # shared in-flight cap (we only get here when headroom allowed the
    # docker matrix), the user-backlog gate, the plan's pause/cancel state
    # (tick returns early for anything not "running"), and the failure
    # brake. A fan-out failure must never break the docker tick.
    _kcal = optional_paid("services.k8s_calibration")
    if _kcal is not None:
        try:
            k8s_task_count = len(_kcal.dispatch_k8s_calibration(
                db, next_node, plan.tenant_id, worker_name))
        except Exception:  # noqa: BLE001
            logger.exception(
                "calibration tick: k8s fan-out failed for node %d; "
                "docker dispatch stands", next_node.id)
    if k8s_task_count:
        plan.last_tick_reason += f"; +{k8s_task_count} k8s pinned run(s)"
        db.commit()

    logger.info(
        "calibration tick: dispatched node %d (%s) — %d runs on %s"
        "%s; in-flight before=%d target=%d",
        next_node.id, next_node.name, len(runs), worker_name,
        f" + {k8s_task_count} k8s run(s)" if k8s_task_count else "",
        in_flight, plan.target_in_flight,
    )
    return {
        "action": "dispatched",
        "node_id": next_node.id,
        "node_name": next_node.name,
        "task_count": len(task_ids),
        "k8s_task_count": k8s_task_count,
        "in_flight_before": in_flight,
        "swept": swept,
    }


# --- sz-tcp.19: size -> runtime regression fit ------------------------------
# tiered-credit-pricing §14: per-node linear trend of runtime against input
# size, stored on NodeDefinition.regression_coefficients so the workflow
# quote can predict a downstream node's size tier. Runs REGARDLESS of cost_basis
# (a manually priced node still needs predictions) — unlike
# scheduler.pattern_updater._auto_calculate_credit_cost, which honours the
# manual/free lock. Pure + deterministic: same DB rows -> same coefficients.
FIT_MIN_SAMPLES = 5
FIT_SIGMA = 3.0


def _task_input_bytes(payload) -> Optional[int]:
    """Total input bytes for a task payload, or None when no signal exists.

    Accepts every shape the queue actually carries: an explicit ``input_bytes``
    / ``input_size_bytes`` scalar, a ``file_sizes`` list/dict (the graph
    builder's shape, same as workflow_orchestrator._compute_input_bytes), or
    the worker's ``inputs`` list of ``{file_size_bytes: ...}`` dicts
    (same shape overlap_scheduler + pattern_updater read).
    """
    if not isinstance(payload, dict):
        return None
    for key in ("input_bytes", "input_size_bytes"):
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            total = int(raw)
        except (TypeError, ValueError):
            continue
        if total > 0:
            return total
    sizes = payload.get("file_sizes")
    if isinstance(sizes, dict):
        values = list(sizes.values())
    elif isinstance(sizes, (list, tuple)):
        values = list(sizes)
    else:
        values = None
    if values is not None:
        total = 0
        for item in values:
            if isinstance(item, bool):
                continue
            try:
                v = int(item)
            except (TypeError, ValueError):
                continue
            if v > 0:
                total += v
        if total > 0:
            return total
    inputs = payload.get("inputs")
    if isinstance(inputs, list):
        total = 0
        for inp in inputs:
            if not isinstance(inp, dict):
                continue
            try:
                v = int(inp.get("file_size_bytes") or 0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                total += v
        if total > 0:
            return total
    return None


def _fit_line(pairs: List[tuple]) -> Optional[dict]:
    """Least-squares fit runtime = a + b*x. Returns {a,b,r2,n} or None.

    ``pairs`` is [(x_bytes, runtime_s), ...]. Deterministic: no RNG, no
    iteration order dependence (inputs are sorted by x before fitting).
    """
    pts = sorted((float(x), float(y)) for x, y in pairs)
    n = len(pts)
    if n < FIT_MIN_SAMPLES:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in pts)
    if sxx <= 0.0:
        return None  # every sample the same size -> slope undefined
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in pts)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0.0 else 1.0
    return {"a": intercept, "b": slope, "r2": r2, "n": n}


def _trim_outliers(pairs: List[tuple]) -> List[tuple]:
    """Drop samples more than FIT_SIGMA standard deviations off the trend.

    tiered-credit-pricing §14: a single thermal-throttle outlier must not pull
    the slope. Never trims below FIT_MIN_SAMPLES (a tight-but-noisy node keeps
    all its samples rather than returning None).
    """
    if len(pairs) < FIT_MIN_SAMPLES:
        return list(pairs)
    fit = _fit_line(pairs)
    if fit is None:
        return list(pairs)
    residuals = [y - (fit["a"] + fit["b"] * x) for x, y in pairs]
    n = len(residuals)
    mean_r = sum(residuals) / n
    var = sum((r - mean_r) ** 2 for r in residuals) / n
    sigma = var ** 0.5
    if sigma <= 0.0:
        return list(pairs)
    kept = [
        (x, y) for (x, y), r in zip(pairs, residuals) if abs(r - mean_r) <= FIT_SIGMA * sigma
    ]
    return kept if len(kept) >= FIT_MIN_SAMPLES else list(pairs)


def _fit_scope_tenant(db: Session, node_def_id, tenant_id, node) -> Optional[int]:
    """Tenant the fit must be scoped to, or None when nothing is measurable.

    A tenant-owned NodeDefinition is unambiguous — its own tenant. A system
    node (tenant_id NULL) is shared by every tenant, so blending their
    hardware into one trend would predict nothing; pin the single tenant that
    produced the most COMPLETED samples for that node (ties -> lowest tenant
    id, so the choice is deterministic).
    """
    from models import ApiTaskQueue, ApiTaskStatus

    if tenant_id is not None:
        return tenant_id
    if node.tenant_id is not None:
        return node.tenant_id

    counts = db.query(ApiTaskQueue.tenant_id).filter(
        ApiTaskQueue.task_payload["node_definition_id"].as_string() == str(node_def_id),
        ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
        ApiTaskQueue.status == ApiTaskStatus.COMPLETED,
    ).all()
    tally: dict = {}
    for (tid,) in counts:
        if tid is None:
            continue
        tally[tid] = tally.get(tid, 0) + 1
    if not tally:
        return None
    return min(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def _collect_fit_samples(db: Session, node_def_id, tenant_id) -> List[tuple]:
    """[(input_bytes, runtime_s), ...] for one tenant's COMPLETED samples.

    Only rows with BOTH an input-size signal and a positive measured duration
    qualify; FAILED / CANCELLED / in-flight rows are never samples (a failure
    is not a measurement).
    """
    from models import ApiTaskQueue, ApiTaskStatus

    tasks = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.task_payload["node_definition_id"].as_string() == str(node_def_id),
        ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
        ApiTaskQueue.status == ApiTaskStatus.COMPLETED,
        ApiTaskQueue.tenant_id == tenant_id,
    ).all()

    pairs: List[tuple] = []
    for task in tasks:
        if not (task.started_at and task.completed_at):
            continue
        duration_s = (task.completed_at - task.started_at).total_seconds()
        if duration_s <= 0:
            continue
        size_bytes = _task_input_bytes(task.task_payload)
        if size_bytes is None:
            continue
        pairs.append((float(size_bytes), float(duration_s)))
    return pairs


def fit_size_and_runtime_regressions(db: Session, node_definition_id,
                                     tenant_id: Optional[int] = None) -> Optional[dict]:
    """Fit runtime_s = a + b*input_bytes for one NodeDefinition (sz-tcp.19).

    Samples are the node's COMPLETED calibration/run ApiTaskQueue rows that
    carry BOTH an input-size signal (task_payload input_bytes / input_size_bytes
    / file_sizes / inputs[].file_size_bytes) and a measurable duration
    (completed_at - started_at). FAILED / CANCELLED / in-flight rows never
    qualify (a failure is not a measurement), and every query is tenant-scoped:
    a tenant-owned node only ever sees its own tenant's samples, a system node
    (tenant_id NULL) is pinned to the single tenant that produced the most
    samples for it so cross-tenant matrices never blend into one trend.

    Returns ``{"a", "b", "r2", "n"}`` (a = intercept s, b = slope s/byte, r2 =
    fit quality, n = post-outlier-trim sample count) or None when fewer than
    FIT_MIN_SAMPLES usable samples exist. Pure + deterministic.

    The fit is persisted on the existing NodeDefinition.regression_coefficients
    JSONB column in the design.md shape (``runtime_seconds``: slope / intercept
    / r_squared / sample_count) so `utils.workflow_credits.compute_workflow_quote`
    can later build a regression-based ``tier_resolver`` off it. Writing is
    skipped (fit still returned) when the tiered-pricing migration has not been
    applied — the column does not exist on such a DB.
    """
    from models import ApiTaskQueue, ApiTaskStatus, NodeDefinition

    node_def_id = getattr(node_definition_id, "id", node_definition_id)
    node = db.query(NodeDefinition).filter(
        NodeDefinition.id == node_def_id
    ).first()
    if node is None:
        return None

    scope_tenant = _fit_scope_tenant(db, node_def_id, tenant_id, node)
    if scope_tenant is None:
        return None

    pairs = _collect_fit_samples(db, node_def_id, scope_tenant)

    if len(pairs) < FIT_MIN_SAMPLES:
        return None

    trimmed = _trim_outliers(pairs)
    fit = _fit_line(trimmed)
    if fit is None:
        return None

    try:
        from utils.node_cost import tier_columns_available
        if tier_columns_available(db):
            from sqlalchemy.orm.attributes import flag_modified
            import models as _models
            from audit import PLATFORM_AUDIT_TENANT_ID

            coefficients = dict(node.regression_coefficients or {})
            coefficients["runtime_seconds"] = {
                "slope": fit["b"],
                "intercept": fit["a"],
                "r_squared": fit["r2"],
                "sample_count": fit["n"],
                "x_unit": "input_bytes",
            }
            node.regression_coefficients = coefficients
            flag_modified(node, "regression_coefficients")

            db.add(_models.AuditEvent(
                tenant_id=node.tenant_id or PLATFORM_AUDIT_TENANT_ID,
                user_id=None,
                event_name="regression.fit",
                details={
                    "node_definition_id": node.id,
                    "tenant_id": scope_tenant,
                    "regression": "runtime_seconds_vs_input_bytes",
                    "slope": fit["b"],
                    "intercept": fit["a"],
                    "r_squared": fit["r2"],
                    "sample_count": fit["n"],
                    "sample_count_raw": len(pairs),
                    "outliers_trimmed": len(pairs) - fit["n"],
                },
            ))
            db.flush()
    except Exception as e:  # noqa: BLE001 — a persist/audit miss must not lose the fit
        logger.warning("regression fit persist for node %s failed: %s", node_def_id, e)

    logger.info(
        "regression fit for node %s (tenant %s): runtime_s = %.6f + %.10g * input_bytes "
        "(r2=%.3f, n=%d/%d)",
        node_def_id, scope_tenant, fit["a"], fit["b"], fit["r2"], fit["n"], len(pairs),
    )
    return fit



def make_regression_tier_resolver(db: Session, node_definition_id,
                                  *, fallback=None):
    """Return a resolver `compute_workflow_quote`'s `tier_resolver` can take.

    sz-tcp.18 documents this plug point as
    ``tier_resolver(node_data) -> (size_tier, confidence)`` and states sz-tcp.19
    supplies a regression-based one "predicting a size tier from historical
    node-run data when no explicit size is given". That is exactly what this
    factory returns, so the quote gains regression tiers by a parameter swap —
    `utils/workflow_credits.py` is untouched.

    Behaviour of the returned resolver:

    * node_data carries a real size signal (file_sizes / input_size_bytes) ->
      the deterministic orchestrator tier, confidence "exact" (a measurement
      always beats a prediction);
    * otherwise the node's own COMPLETED samples are averaged (the same
      tenant-scoped sample set the fit used) and that mean input size is mapped
      through the platform tier thresholds, confidence "estimated";
    * no samples at all -> the deterministic "medium" default, "estimated".

    ``fallback`` replaces that last step with a caller-supplied
    ``(node_data) -> (size_tier, confidence)`` resolver.
    """
    from services.workflow_orchestrator import _determine_size_tier

    # 1788k CE carve: services/cost_estimator* is PAID (credit/cost estimation),
    # so CE has no platform tier-threshold lookup — no cost estimate here.
    # Calibration still proceeds: the resolver falls back to the same
    # documented byte ceilings the paid path defaults to when the settings
    # lookup yields nothing.
    from utils.paid_import import optional_paid
    _cost_estimator = optional_paid("services.cost_estimator")
    thresholds = (_cost_estimator.get_tier_thresholds()
                  if _cost_estimator is not None else {})
    small_max = thresholds.get("small_max_bytes", 2 * 1024 * 1024)
    medium_max = thresholds.get("medium_max_bytes", 30 * 1024 * 1024)

    from models import NodeDefinition

    node_def_id = getattr(node_definition_id, "id", node_definition_id)
    node = db.query(NodeDefinition).filter(
        NodeDefinition.id == node_def_id
    ).first()
    scope_tenant = _fit_scope_tenant(db, node_def_id, None, node) if node else None
    pairs = _collect_fit_samples(db, node_def_id, scope_tenant) if scope_tenant else []
    mean_bytes = (sum(p[0] for p in pairs) / len(pairs)) if pairs else None

    def _resolver(node_data):
        tier, source = _determine_size_tier(node_data)
        if source == "measured":
            return tier, "exact"
        if mean_bytes is not None:
            if mean_bytes <= small_max:
                return "small", "estimated"
            if mean_bytes <= medium_max:
                return "medium", "estimated"
            return "large", "estimated"
        if fallback is not None:
            return fallback(node_data)
        return tier, "estimated"

    return _resolver
