"""Daily targeted drift scan over the scheduler's prediction accuracy.

Aggregates `prediction_logs` MAE per `(node_definition_id,
hardware_profile_id)` pair over a rolling window so drifting pairs can be
re-calibrated individually (scheduler-drift-recal-scan).

`prediction_logs` carries no pair columns, so the pair is derived by joining
`api_task_queue` (node from `task_payload`) and the executing worker's
`worker_licenses.hardware_profile_id`.
"""

from __future__ import annotations

from product_names import themed_logger
import os

from sqlalchemy import text

logger = themed_logger(__name__, 'scheduler')

WINDOW_H_DEFAULT = 24
WINDOW_H_ENV = "SCHEDULER_DRIFT_SCAN_WINDOW_H"
MIN_SAMPLES_DEFAULT = 10
MIN_SAMPLES_ENV = "SCHEDULER_DRIFT_MIN_SAMPLES"
THRESHOLD_PCT_DEFAULT = 25.0
THRESHOLD_PCT_ENV = "SCHEDULER_DRIFT_MAE_THRESHOLD_PCT"

# Requested metric name is `..._total` (the Prometheus counter convention);
# the client library strips that suffix for the collector's internal
# `_name` (it re-adds `_total` only when rendering samples), so the
# lazy-registration lookup below must compare against the stripped form.
_COUNTER_NAME = "scheduler_drift_pairs_flagged_total"
_COUNTER_BASE_NAME = "scheduler_drift_pairs_flagged"

# Module-level Counter — created lazily on first call, mirroring
# `_get_gauge` in scheduler_mae_gauge.py, so test imports don't
# double-register if the registry already holds it.
_counter = None


def _get_counter():
    global _counter
    if _counter is None:
        from prometheus_client import Counter, REGISTRY

        existing = next(
            (c for c in REGISTRY._collector_to_names if getattr(c, "_name", None) == _COUNTER_BASE_NAME),
            None,
        )
        if existing is not None:
            _counter = existing
        else:
            _counter = Counter(
                _COUNTER_NAME,
                "Count of scheduler drift-scan pairs flagged, labeled by "
                "the outcome bucket they landed in (queued / "
                "deferred_capacity_floor / skipped_in_flight / "
                "dispatch_failed).",
                ["outcome"],
            )
    return _counter


def reset_counter_for_tests() -> None:
    """Clear the cached Counter handle so the next access re-discovers or
    re-creates it. Test-only helper."""
    global _counter
    _counter = None


def _window_h() -> int:
    try:
        return int(os.getenv(WINDOW_H_ENV, WINDOW_H_DEFAULT))
    except ValueError:
        return WINDOW_H_DEFAULT


def _min_samples() -> int:
    try:
        return int(os.getenv(MIN_SAMPLES_ENV, MIN_SAMPLES_DEFAULT))
    except ValueError:
        return MIN_SAMPLES_DEFAULT


def _threshold_pct() -> float:
    try:
        return float(os.getenv(THRESHOLD_PCT_ENV, THRESHOLD_PCT_DEFAULT))
    except ValueError:
        return THRESHOLD_PCT_DEFAULT


# Soft-deleted tenant-1 duplicates exist for some node_kinds, so the
# node_definitions join is restricted to live rows to avoid fanning out
# (and double-counting) prediction_logs rows.
_PAIR_MAE_QUERY = text("""
    SELECT coalesce((q.task_payload->>'node_definition_id')::int, nd.id) AS node_definition_id,
           wl.hardware_profile_id AS hardware_profile_id,
           AVG(pl.predicted_duration_s) AS mean_pred,
           AVG(ABS(pl.predicted_duration_s - pl.actual_duration_s)) AS mae,
           COUNT(*) AS samples
    FROM prediction_logs pl
    JOIN api_task_queue q ON q.id = pl.task_id
    JOIN worker_licenses wl
      ON wl.worker_name = coalesce(q.claimed_by_worker, q.assigned_to_worker)
    LEFT JOIN node_definitions nd
      ON nd.node_kind = q.task_payload->>'node_kind'
     AND nd.tenant_id = 1
     AND nd.deleted_at IS NULL
    WHERE pl.completed_at > NOW() - make_interval(hours => :window_h)
      AND pl.actual_duration_s IS NOT NULL
      AND q.status = 'COMPLETED'
    GROUP BY 1, 2
""")


def aggregate_pair_mae(db, window_h: int) -> list[dict]:
    """Per-(node_definition_id, hardware_profile_id) MAE over the past
    ``window_h`` hours. No sample/threshold filtering — callers classify."""
    rows = db.execute(_PAIR_MAE_QUERY, {"window_h": int(window_h)}).fetchall()
    return [
        {
            "node_definition_id": r.node_definition_id,
            "hardware_profile_id": r.hardware_profile_id,
            "mean_pred": float(r.mean_pred) if r.mean_pred is not None else None,
            "mae": float(r.mae) if r.mae is not None else None,
            "samples": int(r.samples),
        }
        for r in rows
    ]


# Calibration rows are marked by `task_payload.calibration_run = true`
# (routers.node_definitions_domains._shared._enqueue_calibration_task). The
# pair's profile comes from the payload (pair-scoped dispatch) or, failing
# that, from the pinned/claiming worker. Payload keys are compared as text so
# a malformed payload elsewhere in the queue can't break the scan with a cast.
# `status` is a PG enum stored by member NAME (uppercase).
_PAIR_RECENTLY_CALIBRATED_QUERY = text("""
    SELECT 1
    FROM api_task_queue q
    LEFT JOIN worker_licenses wl
      ON wl.worker_name = coalesce(q.claimed_by_worker, q.assigned_to_worker)
    WHERE q.task_payload->>'calibration_run' = 'true'
      AND q.task_payload->>'node_definition_id' = :node_definition_id
      AND coalesce(q.task_payload->>'hardware_profile_id',
                   wl.hardware_profile_id::text) = :hardware_profile_id
      AND (
            q.status IN ('PENDING', 'ASSIGNED', 'CLAIMED', 'RUNNING')
         OR (q.status = 'COMPLETED'
             AND coalesce(q.completed_at, q.updated_at)
                 > NOW() - make_interval(hours => :window_h))
      )
    LIMIT 1
""")


def pair_recently_calibrated(db, node_definition_id: int, hardware_profile_id: int,
                             window_h: int = WINDOW_H_DEFAULT) -> bool:
    """True if a calibration task for the pair is in flight
    (PENDING/ASSIGNED/CLAIMED/RUNNING) or COMPLETED within ``window_h``."""
    row = db.execute(_PAIR_RECENTLY_CALIBRATED_QUERY, {
        "node_definition_id": str(int(node_definition_id)),
        "hardware_profile_id": str(int(hardware_profile_id)),
        "window_h": int(window_h),
    }).first()
    return row is not None


def _mae_pct(pair: dict):
    """Relative MAE (% of the mean prediction) or None when undefined."""
    mae, mean_pred = pair.get("mae"), pair.get("mean_pred")
    if mae is None or not mean_pred or mean_pred <= 0:
        return None
    return mae / mean_pred * 100.0


def run_drift_scan(db, *, capacity_floor_pct: int | None = None) -> dict:
    """Classify every (node_definition_id, hardware_profile_id) pair over the
    rolling window and queue a pair-scoped re-calibration for drifting pairs.

    Order: sample floor -> MAE threshold -> in-flight/recent calibration ->
    capacity gate. A pair past the sample floor and over the threshold is
    "flagged" and gets one SCHEDULER_DRIFT_DETECTED audit row carrying its
    ``outcome``. When the capacity gate denies, every remaining flagged pair
    is deferred (no dispatch attempted); the next daily scan retries.

    ``capacity_floor_pct`` overrides the runtime-config floor (tests).
    """
    import models
    from services.calibration_dispatcher import (
        auto_queue_for_node_profile_pair,
        should_dispatch_calibration,
    )

    window_h = _window_h()
    min_samples = _min_samples()
    threshold_pct = _threshold_pct()

    summary = {
        "queued": 0,
        "deferred_capacity_floor": 0,
        "skipped_in_flight": 0,
        "skipped_below_threshold": 0,
        "skipped_below_samples": 0,
        "dispatch_failed": 0,
    }
    gate_open = None

    for pair in aggregate_pair_mae(db, window_h):
        nd_id = pair["node_definition_id"]
        hp_id = pair["hardware_profile_id"]
        if nd_id is None or hp_id is None:
            continue

        if pair["samples"] < min_samples:
            summary["skipped_below_samples"] += 1
            continue

        mae_pct = _mae_pct(pair)
        if mae_pct is None or mae_pct <= threshold_pct:
            summary["skipped_below_threshold"] += 1
            continue

        task_ids: list = []
        if pair_recently_calibrated(db, nd_id, hp_id, window_h):
            outcome = "skipped_in_flight"
        else:
            if gate_open is None:
                gate_open, reason = should_dispatch_calibration(
                    db, capacity_floor_pct=capacity_floor_pct)
                if not gate_open:
                    logger.info("drift scan: capacity gate closed: %s", reason)
            if not gate_open:
                outcome = "deferred_capacity_floor"
            else:
                res = auto_queue_for_node_profile_pair(
                    db, node_definition_id=nd_id, hardware_profile_id=hp_id,
                    trigger_source="drift_scan",
                    capacity_floor_pct=capacity_floor_pct,
                )
                if res:
                    outcome = "queued"
                    task_ids = list(res.get("task_ids") or [])
                else:
                    # Our own dispatches can consume the headroom mid-scan;
                    # re-check so a floor refusal is reported as a deferral.
                    still_open, _ = should_dispatch_calibration(
                        db, capacity_floor_pct=capacity_floor_pct)
                    if still_open:
                        outcome = "dispatch_failed"
                    else:
                        gate_open = False
                        outcome = "deferred_capacity_floor"

        summary[outcome] += 1
        try:
            _get_counter().labels(outcome=outcome).inc()
        except Exception as e:  # noqa: BLE001
            logger.debug("scheduler drift scan: metrics increment failed (non-fatal): %s", e)
        db.add(models.AuditEvent(
            event_name="SCHEDULER_DRIFT_DETECTED",
            details={
                "node_definition_id": nd_id,
                "hardware_profile_id": hp_id,
                "mae_s": round(pair["mae"], 3),
                "mean_pred_s": round(pair["mean_pred"], 3),
                "mae_pct": round(mae_pct, 2),
                "threshold_pct": threshold_pct,
                "samples": pair["samples"],
                "window_h": window_h,
                "task_ids": task_ids,
                "outcome": outcome,
            },
        ))

    db.commit()
    logger.info("scheduler drift scan: %s", summary)
    return summary
