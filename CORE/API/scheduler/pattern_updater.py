"""
Pattern Updater — classifies resource samples into ResourcePattern entries.

Called after each task completion to update the pattern database with the latest
execution data. Patterns are used by the OverlapScheduler to predict future
resource curves and compute overlap windows.
"""

import logging
from product_names import themed_logger
import sys
import math
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from utils.paid_import import optional_paid

from models import (
    ApiTaskQueue, ResourcePattern, TaskResourceSample,
)

logger = themed_logger(__name__, 'scheduler')


def _commit_or_flush(db: Session, commit: bool) -> None:
    """Persist pending writes, honouring the D5 batch boundary.

    ``commit=True`` (default, today's per-completion path) commits immediately.
    ``commit=False`` (batched telemetry path) only FLUSHES — the pending changes
    are pushed to the DB so subsequent queries in the same session see them, but
    the transaction stays open so the whole batch commits ONCE at the end (see
    ``_flush_pattern_updates``). This is the only behavioural knob the batching
    adds to the write path; everything else is unchanged.
    """
    if commit:
        db.commit()
    else:
        db.flush()


def update_pattern_from_task(task: ApiTaskQueue, db: Session, commit: bool = True):
    """
    After a task completes, query its resource samples, classify the pattern,
    and upsert the ResourcePattern table.

    ``commit`` (scale-tiered-workloads D5): when True (default) this commits its
    own writes exactly as before — the per-completion write path we ship today,
    so existing callers are unaffected. The batched telemetry entrypoint
    (:func:`enqueue_pattern_update`) passes ``commit=False`` so that many
    completions processed together commit ONCE at the end of the batch instead
    of once per completion, dropping the DB write rate below one write per event.
    """
    node_kind = _extract_node_kind(task)
    if not node_kind:
        logger.debug("No node_kind for task %d, skipping pattern update", task.id)
        return

    # Fetch all samples for this task
    samples = db.query(TaskResourceSample).filter(
        TaskResourceSample.task_id == task.id
    ).order_by(TaskResourceSample.timestamp.asc()).all()

    if len(samples) < 1:
        logger.debug("Task %d has no resource samples, skipping pattern update", task.id)
        return

    # Group samples by phase
    samples_by_phase = {}
    for s in samples:
        samples_by_phase.setdefault(s.phase, []).append(s)

    # Classify the processing phase pattern (this is the main resource curve)
    processing_samples = samples_by_phase.get("processing", [])
    pattern_result = _classify_processing_pattern(processing_samples)

    # Build per-phase profiles (duration, peak resources, avg resources)
    phase_profiles = _build_phase_profiles(samples_by_phase)

    # Compute aggregate stats
    task_duration = None
    if task.started_at and task.completed_at:
        task_duration = (task.completed_at - task.started_at).total_seconds()

    peak_ram = max((s.ram_mb or 0) for s in samples) if samples else None
    peak_vram = max((s.vram_mb or 0) for s in samples) if samples else None
    # A non-GPU queue's tasks have no GPU to consume, so their VRAM demand is
    # 0 by definition.  Observed live 2026-10-09: build tasks (queue CPU)
    # recorded avg_peak_vram_mb 39542 because the worker's resource samples
    # report the box's GPU state, not the task's usage.  That poisoned
    # pattern then made the scheduler plan vram_mb=39542 for a CPU task,
    # every worker's validate-phase check failed, and the claim/revert loop
    # burned scheduling_version ~1.4x/second with the task never STARTING.
    # The queue is the authority here, so clamp at the source.
    if peak_vram and (task.queue_name or "").strip().lower() not in (
            "cuda", "rocm", "intel", "npu", "accelerator", "gpu"):
        logger.debug(
            "Task %d on non-GPU queue %s: VRAM peak %.0fMB ignored for pattern "
            "%s (no GPU on this queue)",
            task.id, task.queue_name, peak_vram, node_kind,
        )
        peak_vram = 0.0

    # Extract input/output sizes from task payload
    input_size_mb = _extract_input_size(task)
    output_size_mb = _extract_output_size(task)

    # Determine size tier
    size_tier = _determine_size_tier(task)

    # Upsert ResourcePattern
    existing = db.query(ResourcePattern).filter(
        ResourcePattern.node_kind == node_kind,
        ResourcePattern.queue_name == task.queue_name,
        ResourcePattern.size_tier == size_tier,
    ).first()

    # A contended run measures a different quantity than an isolated one, and
    # merging them destroys both.
    #
    # `avg_duration_seconds` is consumed as "how long will this task take",
    # which must be the UNCONTENDED estimate — the scheduler models contention
    # itself from the timeline. But the identity of a pattern row is only
    # (node_kind, queue_name, size_tier), so contention runs were landing in
    # the same row as isolation runs and averaging with them. Measured on node
    # 1 at the small tier: isolation 10.8s, pairwise 31.5s, multi 40.7s, all
    # pooled into a single stored 27.4s. The isolated truth is 10.8s.
    #
    # At full-matrix scale it gets worse, not better: the matrix is 9 isolation
    # runs against 75 contended ones, so the stored duration converges toward
    # the contended value — roughly 3.7x the truth — and the scheduler
    # under-packs every worker by that factor. The contention signal is lost in
    # the same stroke, since nothing can separate the two afterwards.
    #
    # So contended runs keep their own aggregates under pattern_data
    # ["contention"], keyed by stressor set, and leave the baseline alone.
    _payload = task.task_payload or {}
    _run_kind = _payload.get("calibration_run_kind")

    if _run_kind == "invalid_contention":
        # The stressor could not be confirmed, so this is neither a contention
        # measurement nor a clean isolation one. Recording it either way would
        # be a guess about how much foreign load it actually saw.
        logger.info(
            "Task %d: contention unproven, excluded from pattern %s/%s/%s",
            task.id, node_kind, task.queue_name, size_tier,
        )
        return

    if _run_kind in ("pairwise", "multi"):
        if not existing:
            # Isolation runs are dispatched first by design, so a baseline
            # should already exist. Creating one from a contended run would
            # install a deliberately-degraded duration as the node's normal.
            logger.warning(
                "Task %d: contended run for %s/%s/%s with no isolation "
                "baseline yet — skipping rather than seeding a degraded one",
                task.id, node_kind, task.queue_name, size_tier,
            )
            return
        _record_contention(existing, _payload, task_duration,
                           peak_ram, peak_vram)
        _commit_or_flush(db, commit)  # D5: defer to batch boundary when batched
        logger.info(
            "Recorded %s contention for %s/%s/%s: %.1fs against a %.1fs baseline",
            _run_kind, node_kind, task.queue_name, size_tier,
            task_duration or 0.0, existing.avg_duration_seconds or 0.0,
        )
        return

    if existing:
        _is_cal = bool(_payload.get("calibration_run"))
        _merge_pattern(existing, pattern_result, phase_profiles,
                       task_duration, peak_ram, peak_vram,
                       input_size_mb, output_size_mb,
                       is_calibration=_is_cal)
    else:
        existing = ResourcePattern(
            node_kind=node_kind,
            queue_name=task.queue_name,
            size_tier=size_tier,
            pattern_type=pattern_result.get("pattern", "unknown"),
            pattern_data=pattern_result,
            phase_profiles=phase_profiles,
            sample_count=1,
            avg_duration_seconds=task_duration,
            avg_peak_ram_mb=peak_ram,
            avg_peak_vram_mb=peak_vram,
            avg_input_size_mb=input_size_mb,
            avg_output_size_mb=output_size_mb,
            confidence=pattern_result.get("confidence", 0.0),
        )
        db.add(existing)

    # 18.4 (GI.4): a k8s-pinned calibration task files its duration under the
    # named node in the SAME store (pattern_data["k8s_nodes"]) that 18.6's
    # per-node routing reads. Runs after both upsert branches, so the row
    # exists either way; the contended-run early returns above already
    # excluded the measurements this must not see.
    _record_k8s_node_duration(existing, task, task_duration, db)

    _commit_or_flush(db, commit)  # D5: defer to batch boundary when batched
    logger.info(
        "Updated pattern for %s/%s/%s: type=%s confidence=%.2f (sample_count=%d)",
        node_kind, task.queue_name, size_tier,
        existing.pattern_type, existing.confidence, existing.sample_count,
    )

    # Backfill PredictionLog with the actual outcome so the auto-retrain
    # trigger has a real signal to drive on. Failures here are non-fatal —
    # the pattern update itself already succeeded.
    try:
        _backfill_prediction_log(task, db, task_duration, pattern_result, commit=commit)
    except Exception as e:
        logger.debug("PredictionLog backfill failed for task %d (non-fatal): %s", task.id, e)

    _check_calibration_complete(task, db, commit=commit)


def _record_k8s_node_duration(pattern: ResourcePattern, task: ApiTaskQueue,
                              duration: Optional[float], db: Session) -> None:
    """18.4 (GI.4) — file a k8s-pinned calibration task's duration under the
    named node in the shared calibration store:

        pattern_data["k8s_nodes"][<node>] = {"avg_duration_seconds": N,
                                             "samples": N}

    Equal-weight RUNNING AVERAGE across completions — exactly the treatment
    the docker path gives calibration repeats (replicates exist to average
    noise out; a recency weight would track whichever replicate landed
    last). 18.6's ``k8s_node_routing.load_k8s_node_durations`` reads this
    key (it averages a node's entries across the kind's pattern rows, so
    repeated calibrations accumulate).

    Only BASELINE (isolation) measurements count: contended runs measure the
    stressor, not the node. Never raises — a routing-data write must never
    break the pattern upsert it rides on."""
    if duration is None or duration <= 0:
        return
    try:
        payload = task.task_payload or {}
        if not payload.get("calibration_run"):
            return
        run_kind = payload.get("calibration_run_kind")
        if run_kind in ("pairwise", "multi", "invalid_contention"):
            return

        _kcal = optional_paid("services.k8s_calibration")
        if _kcal is None:
            return
        node_name = _kcal.k8s_pin_for_task(db, task)
        if not node_name:
            return

        # The pin is a REQUIRED node placement: the duration is only
        # meaningful if it was measured ON the named node, which only a
        # k8s Job can guarantee. A pinned task that completed on a docker
        # worker (e.g. the executor backend flipped mid-run) would file a
        # docker duration under a k8s node name and poison the routing
        # data 18.6 reads — so only file it when this task's tenant is
        # actually running tasks as k8s Jobs.
        from utils.k8s_backend_config import BACKEND_K8S
        from services import k8s_backend_gate as _gate
        if _gate.effective_executor_backend(db, task.tenant_id) != BACKEND_K8S:
            return

        data = dict(pattern.pattern_data or {})
        k8s_nodes = dict(data.get("k8s_nodes") or {})
        entry = k8s_nodes.get(node_name)
        if not isinstance(entry, dict):
            entry = {}
        try:
            samples = int(entry.get("samples") or 0)
        except (TypeError, ValueError):
            samples = 0
        old = entry.get("avg_duration_seconds")
        try:
            old = float(old) if old is not None else None
        except (TypeError, ValueError):
            old = None
        if samples > 0 and old and old > 0:
            avg = (old * samples + float(duration)) / (samples + 1)
        else:
            avg = float(duration)
        k8s_nodes[node_name] = {
            "avg_duration_seconds": round(avg, 3),
            "samples": samples + 1,
        }
        data["k8s_nodes"] = k8s_nodes
        pattern.pattern_data = data
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(pattern, "pattern_data")
        logger.info(
            "Recorded k8s node duration for %s on %s: %.1fs (sample %d)",
            pattern.node_kind, node_name, avg, samples + 1,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "k8s node duration write failed for task %s (non-fatal)",
            task.id, exc_info=True)


# =============================================================================
# Batched pattern updates (scale-tiered-workloads, decision D5 — phase 6)
# =============================================================================
# A pattern update runs on EVERY task completion, on the /complete request path,
# and today it commits per completion (the "Updated pattern for .../.../..." hot
# path). Under a heavy fan-out run that is a high-frequency write storm on a
# latency-sensitive path. `enqueue_pattern_update` is the entrypoint the router
# uses instead of calling `update_pattern_from_task` directly:
#
#   * MOD_TELEMETRY_BATCHING off (default): run inline right now, committing per
#     completion — byte-for-byte today's behaviour, so shipping this changes
#     nothing until an operator opts in.
#   * MOD_TELEMETRY_BATCHING on: buffer the completed task id and process the
#     buffer off the request path via the shared TelemetryBatcher, reloading the
#     tasks in one fresh session and committing ONCE for the whole batch
#     (N completions → 1 commit).
#
# We buffer only the integer task id — never the ORM `task` object — so nothing
# is shared across sessions/threads. The shared primitive (utils.queue_state)
# guarantees no data loss: it flushes on the size threshold, on its periodic
# timer, and on interpreter shutdown (atexit / flush_all_batchers).


def enqueue_pattern_update(task: ApiTaskQueue, db: Session) -> None:
    """Route a completed task's pattern update through the D5 batcher (or inline
    when batching is off). Safe to call from the request path.

    This is the call site the completion path should use in place of a direct
    ``update_pattern_from_task`` call.
    """
    from utils.queue_state import telemetry_batching_enabled
    if not telemetry_batching_enabled():
        # Unchanged hot path: classify + upsert + commit right here, on the
        # caller's session, exactly as before D5.
        update_pattern_from_task(task, db)
        return
    # Batched: hand off just the id; the flush reloads it in its own session.
    _PATTERN_BATCHER.add(int(task.id))


def _flush_pattern_updates(task_ids: list) -> None:
    """Process a batch of buffered completions in one fresh session, committing
    once for the whole batch (D5).

    Invoked by the TelemetryBatcher on the size threshold, the periodic timer,
    or the shutdown flush — never on the request path.
    """
    from database import SessionLocal

    # Dedupe while preserving order: a task can only be completed once, but a
    # retry storm could enqueue the same id twice within a flush window.
    seen = set()
    ordered_ids = [t for t in task_ids if not (t in seen or seen.add(t))]

    db = SessionLocal()
    try:
        for tid in ordered_ids:
            task = db.query(ApiTaskQueue).filter(ApiTaskQueue.id == tid).first()
            if task is None:
                continue
            try:
                # commit=False: accumulate every write, then one commit below.
                update_pattern_from_task(task, db, commit=False)
            except Exception as e:
                # One bad completion must not sink the rest of the batch.
                logger.warning("Batched pattern update failed for task %d: %s", tid, e)
        db.commit()
    except Exception as e:
        logger.error("Batched pattern-update flush failed (%d task(s)): %s",
                     len(ordered_ids), e)
        db.rollback()
    finally:
        db.close()


def _make_pattern_batcher():
    """Construct the shared batcher lazily so import-time has no hard dependency
    on utils.queue_state (and so tests can stub it). The TelemetryBatcher
    registers itself for the atexit/shutdown flush, so no completion is lost on
    process stop."""
    from utils.queue_state import TelemetryBatcher
    return TelemetryBatcher("pattern_updater.pattern", _flush_pattern_updates)


_PATTERN_BATCHER = _make_pattern_batcher()


def _backfill_prediction_log(task, db: Session, actual_duration_s, pattern_result, commit: bool = True):
    """Find the PredictionLog row written at scheduling time and fill in actuals.

    Computes prediction_error_pct as |predicted - actual| / actual * 100.
    Skipped silently when no row exists (task wasn't placed via the packer
    path — e.g. legacy direct-dispatch tasks).

    ``commit`` (D5): batched path passes False so this write joins the single
    end-of-batch commit rather than committing on its own.
    """
    from datetime import datetime, timezone
    from models import PredictionLog

    row = db.query(PredictionLog).filter(
        PredictionLog.task_id == task.id,
        PredictionLog.actual_duration_s.is_(None),
    ).order_by(PredictionLog.predicted_at.desc()).first()
    if row is None:
        return

    actual = float(actual_duration_s) if actual_duration_s else None
    row.actual_duration_s = actual
    row.actual_pattern = str(pattern_result.get("pattern", "unknown")) if pattern_result else "unknown"
    row.completed_at = datetime.now(timezone.utc)
    if actual and row.predicted_duration_s and actual > 0:
        err = abs(row.predicted_duration_s - actual) / actual * 100.0
        row.prediction_error_pct = err
    _commit_or_flush(db, commit)  # D5: defer to batch boundary when batched
    logger.info(
        "PredictionLog backfilled for task %d: predicted=%.1fs actual=%.1fs error=%.1f%%",
        task.id,
        row.predicted_duration_s or 0.0,
        actual or 0.0,
        row.prediction_error_pct or 0.0,
    )


def _classify_processing_pattern(samples: list) -> dict:
    """Classify the processing phase samples into a resource pattern."""
    if len(samples) < 3:
        return {"pattern": "unknown", "confidence": 0.0, "metrics": {}}

    try:
        from utils.pattern_recognition import classify_resource_pattern

        # Convert ORM samples to the format expected by classify_resource_pattern
        sample_dicts = []
        for s in samples:
            sample_dicts.append({
                "t": s.timestamp.isoformat() if s.timestamp else "",
                "ram_mb": s.ram_mb or 0,
                "cpu": s.cpu_percent or 0,
                "vram_mb": s.vram_mb or 0,
            })

        result = classify_resource_pattern(sample_dicts, resource_key="ram_mb")
        return result if result else {"pattern": "unknown", "confidence": 0.0, "metrics": {}}

    except ImportError:
        logger.warning("pattern_recognition module not available, using fallback")
        return _fallback_classify(samples)
    except Exception as e:
        logger.warning("Pattern classification failed: %s", e)
        return {"pattern": "unknown", "confidence": 0.0, "metrics": {}}


def _fallback_classify(samples: list) -> dict:
    """Simple fallback classification when pattern_recognition is unavailable."""
    if not samples:
        return {"pattern": "unknown", "confidence": 0.0, "metrics": {}}

    ram_values = [s.ram_mb for s in samples if s.ram_mb]
    if len(ram_values) < 3:
        return {"pattern": "unknown", "confidence": 0.0, "metrics": {}}

    # Simple heuristic: check if RAM is roughly stable (plateau) or ramping
    min_v, max_v = min(ram_values), max(ram_values)
    mean_v = sum(ram_values) / len(ram_values)
    spread = (max_v - min_v) / mean_v if mean_v > 0 else 0

    if spread < 0.2:
        return {
            "pattern": "plateau",
            "confidence": 0.6,
            "metrics": {"plateau_value": mean_v, "initial_value": ram_values[0]},
        }
    elif ram_values[-1] > ram_values[0] * 1.3:
        return {
            "pattern": "linear_ramp",
            "confidence": 0.5,
            "metrics": {"start_value": ram_values[0], "end_value": ram_values[-1]},
        }
    else:
        return {"pattern": "unknown", "confidence": 0.3, "metrics": {}}


def _build_phase_profiles(samples_by_phase: Dict[str, list]) -> dict:
    """Build resource profiles for each lifecycle phase."""
    profiles = {}
    for phase, samples in samples_by_phase.items():
        if not samples:
            continue

        timestamps = [s.timestamp for s in samples if s.timestamp]
        duration = None
        if len(timestamps) >= 2:
            duration = (max(timestamps) - min(timestamps)).total_seconds()

        ram_values = [s.ram_mb for s in samples if s.ram_mb]
        cpu_values = [s.cpu_percent for s in samples if s.cpu_percent]
        vram_values = [s.vram_mb for s in samples if s.vram_mb]
        net_rx = [s.net_rx_mbps for s in samples if s.net_rx_mbps]
        net_tx = [s.net_tx_mbps for s in samples if s.net_tx_mbps]
        disk_r = [s.disk_read_iops for s in samples if s.disk_read_iops]
        disk_w = [s.disk_write_iops for s in samples if s.disk_write_iops]

        profiles[phase] = {
            "sample_count": len(samples),
            "duration_s": duration,
            "ram": _stats(ram_values),
            "cpu": _stats(cpu_values),
            "vram": _stats(vram_values),
            "net_rx_mbps": _stats(net_rx),
            "net_tx_mbps": _stats(net_tx),
            "disk_read_iops": _stats(disk_r),
            "disk_write_iops": _stats(disk_w),
        }

    return profiles


def _stats(values: list) -> Optional[dict]:
    """Compute min/max/avg/peak for a list of numeric values."""
    if not values:
        return None
    return {
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values),
        "peak": max(values),
    }


def _merge_pattern(existing: ResourcePattern, new_pattern: dict, new_profiles: dict,
                   duration: Optional[float], peak_ram: Optional[float],
                   peak_vram: Optional[float], input_size: Optional[float],
                   output_size: Optional[float], is_calibration: bool = False):
    """
    Merge new execution data into an existing pattern.

    Production telemetry uses a RECENCY-BIASED average: a recent execution
    says more about current reality than an old one, so it gets up to 3x the
    weight of history.

    Calibration is the opposite case and must not use it. Its repeats are
    deliberate replicates — the same node, same tier, run N times specifically
    to average noise out. Weighting the last one 3x defeats the point: it
    makes the stored value track whichever replicate happened to run last
    rather than the central tendency. Measured on identical work, isolation
    runs varied 11.8s / 16.4s / 14.6s — a 39% spread — so which replicate
    lands last materially changes the answer.

    Calibration therefore gets EQUAL weighting.
    """
    n = existing.sample_count or 0
    if is_calibration:
        new_weight = 1
    else:
        # Recency weight: new sample gets weight equal to last 3 samples combined
        new_weight = min(3, max(1, n // 3))
    total = n + new_weight

    # Update pattern type if the new classification has higher confidence
    if new_pattern.get("confidence", 0) > (existing.confidence or 0) * 0.8:
        existing.pattern_type = new_pattern.get("pattern", existing.pattern_type)
        new_data = dict(new_pattern)
        # 18.4 (GI.4): per-k8s-node durations ride in pattern_data
        # ["k8s_nodes"] — a reclassification replaces the rest of the dict
        # and must not wipe the store 18.6's routing reads (or the
        # per-node running average written after this merge).
        old_data = existing.pattern_data
        if isinstance(old_data, dict) and "k8s_nodes" in old_data:
            new_data["k8s_nodes"] = old_data["k8s_nodes"]
        existing.pattern_data = new_data

    # Weighted average for aggregate stats
    if duration is not None:
        old = existing.avg_duration_seconds or duration
        existing.avg_duration_seconds = (old * n + duration * new_weight) / total

    if peak_ram is not None:
        old = existing.avg_peak_ram_mb or peak_ram
        existing.avg_peak_ram_mb = (old * n + peak_ram * new_weight) / total

    if peak_vram is not None:
        old = existing.avg_peak_vram_mb or peak_vram
        existing.avg_peak_vram_mb = (old * n + peak_vram * new_weight) / total

    if input_size is not None:
        old = existing.avg_input_size_mb or input_size
        existing.avg_input_size_mb = (old * n + input_size * new_weight) / total

    if output_size is not None:
        old = existing.avg_output_size_mb or output_size
        existing.avg_output_size_mb = (old * n + output_size * new_weight) / total

    # Update confidence: weighted blend of old and new
    new_conf = new_pattern.get("confidence", 0.0)
    existing.confidence = (existing.confidence * n + new_conf * new_weight) / total

    existing.phase_profiles = new_profiles  # Always use latest phase profiles
    existing.sample_count = n + 1


def _determine_size_tier(task: ApiTaskQueue) -> str:
    """Determine size tier from task payload. Calibration tasks have explicit tier.

    sz-tcp.26 — the tier logic is no longer duplicated here: we build the
    ``node_data`` dict the orchestrator expects (the fields this function reads
    today: ``task_payload["size_tier"]`` short-circuit, ``task_payload["inputs"]
    [].file_size_bytes`` byte totals, plus the new ``task_payload["size_probe"]``
    probe result) and hand it to
    ``services.workflow_orchestrator._determine_size_tier``.

    Two things stay pattern-side because they are part of the ResourcePattern
    contract, not the credit-tier contract:

    * the explicit ``size_tier`` on a calibration task payload wins as-is;
    * a task with **no size signal at all** maps to ``"any"`` (the wildcard
      pattern bucket) — the orchestrator answers ``("medium", "default")``
      there, which would silently merge unknown-size runs into the medium
      bucket and double-count them against real medium runs.

    Byte-only tasks keep this module's historical bucket boundaries (100 MB /
    2048 MB) — they are the calibration buckets the existing ResourcePattern
    rows were keyed by, a different scale from the credit-tier ceilings
    (``pricing.credit_tier_*_max_bytes``, 2 MB / 30 MB). Probed tasks take the
    orchestrator's format-family answer verbatim.
    """
    if task.task_payload:
        explicit = task.task_payload.get("size_tier")
        if explicit in ("small", "medium", "large"):
            return explicit

    probe_tier = _probe_tier_via_orchestrator(task)
    if probe_tier is not None:
        return probe_tier

    input_size = _extract_input_size(task)
    if input_size is None:
        return "any"
    if input_size < 100:
        return "small"
    if input_size < 2048:
        return "medium"
    return "large"


def _probe_tier_via_orchestrator(task: ApiTaskQueue) -> Optional[str]:
    """Ask the orchestrator's ``_determine_size_tier`` for a probe-derived tier.

    Returns the tier string only when the orchestrator classified from a
    ``size_probe`` result (``size_tier_source == "probe"``); ``None`` means
    "no usable probe — let the caller fall back to byte counting", which keeps
    byte-only behaviour exactly as it was. Never raises: this runs on the
    post-completion telemetry sweep, where a bad probe payload or a missing
    settings registry must not knock the sweep over.
    """
    payload = task.task_payload or {}
    probe = payload.get("size_probe")
    if not isinstance(probe, dict):
        return None

    # Build the node_data shape the orchestrator reads: `file_sizes` (per-file
    # byte counts) and the probe itself. Deliberately minimal — the
    # orchestrator's byte fallback sums every string in node_data, so passing
    # the whole payload would invent a byte signal out of node_kind/labels.
    node_data = {"size_probe": probe}
    file_sizes = [
        inp.get("file_size_bytes", 0)
        for inp in payload.get("inputs", [])
        if isinstance(inp, dict)
    ]
    if file_sizes:
        node_data["file_sizes"] = file_sizes

    try:
        # Lazy import: services.workflow_orchestrator is heavy (models, routers)
        # and pattern_updater is imported by the scheduler before those units
        # exist — a module-level import would be circular.
        from services.workflow_orchestrator import _determine_size_tier as _orchestrator_tier

        tier, source = _orchestrator_tier(node_data)
    except Exception:  # noqa: BLE001 — telemetry path: fall back to bytes
        logger.debug("orchestrator size-tier probe failed for task %s", task.id)
        return None
    if source != "probe" or tier not in ("small", "medium", "large"):
        return None
    return tier


def _check_calibration_complete(task: ApiTaskQueue, db: Session, commit: bool = True):
    """After a calibration task completes, update the NodeDefinition progress.

    ``commit`` (D5): batched path passes False so this write joins the single
    end-of-batch commit rather than committing on its own.
    """
    if not task.task_payload or not task.task_payload.get("calibration_run"):
        return
    # 18.4 (GI.4): k8s per-node fan-out runs ride the calibration_run marker
    # (so the in-flight cap / brake / cancellation see them) but are NOT part
    # of the 84-task matrix — counting them would drive calibration_progress
    # past `total` and mark the node calibrated early.
    try:
        _kcal = optional_paid("services.k8s_calibration")
        if _kcal is not None and _kcal.k8s_pin_for_task(db, task):
            return
    except Exception:  # noqa: BLE001 — the counter must still run
        pass
    node_def_id = task.task_payload.get("node_definition_id")
    if not node_def_id:
        return
    try:
        from models import NodeDefinition
        node_def = db.query(NodeDefinition).filter(NodeDefinition.id == node_def_id).first()
        if not node_def or node_def.calibration_status != "calibrating":
            return
        # Fall back to TOTAL_CALIBRATION_TASKS (84 — see Bucket E matrix
        # expansion) when the node's calibration_progress hasn't been
        # initialised yet. Older rows that still have total=9 are honoured
        # as-is so already-running batches don't change shape mid-flight.
        try:
            from routers.node_definitions import TOTAL_CALIBRATION_TASKS as _TOTAL
        except Exception:
            _TOTAL = 84
        progress = node_def.calibration_progress or {"completed": 0, "total": _TOTAL}
        progress["completed"] = progress.get("completed", 0) + 1
        node_def.calibration_progress = progress
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(node_def, "calibration_progress")
        if progress["completed"] >= progress["total"]:
            node_def.calibration_status = "calibrated"
            node_def.last_calibrated_at = datetime.now(timezone.utc)
            logger.info("Node %d calibration complete (%d/%d)", node_def_id, progress["completed"], progress["total"])
            _auto_calculate_credit_cost(node_def, db)
            # sz-tcp.19 — the SAME post-completion sweep also fits the size->runtime
            # regression. Unlike the cost recompute above, this fit runs
            # regardless of cost_basis: a manual/free-priced node still needs
            # coefficients so the workflow quote can predict its tier.
            # Exception-safe on its own — a fit failure must never break the
            # sweep (the calibration counter + retrain bump below still fire).
            try:
                from services.calibration_coordinator import (
                    fit_size_and_runtime_regressions as _fit_regressions,
                )
                _fit = _fit_regressions(db, node_def.id)
                logger.info(
                    "regression fit for node %d on calibration complete: %s",
                    node_def_id, _fit,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "size/runtime regression fit failed for node %s: %s", node_def_id, e
                )
            # Bucket C — increment the auto-retrain trigger counter. Every
            # node-calibration completion adds new training data; once the
            # configured threshold of completions accumulates (default 3),
            # the daily auto-retrain eval job fires the retrain pipeline.
            _increment_retrain_counter(db)
        _commit_or_flush(db, commit)  # D5: defer to batch boundary when batched
    except Exception as e:
        logger.warning("Failed to update calibration progress for node %s: %s", node_def_id, e)


def _increment_retrain_counter(db: Session) -> int:
    """Bump prediction_training_marker.recalibrations_since_last_train by one.

    Auto-creates the singleton row if missing (defensive — the migration
    seeds it but a fresh DB without the migration applied would otherwise
    silently no-op). Returns the new counter value for caller logging.
    """
    from models import PredictionTrainingMarker

    marker = db.query(PredictionTrainingMarker).filter(
        PredictionTrainingMarker.id == 1
    ).first()
    if marker is None:
        marker = PredictionTrainingMarker(id=1, recalibrations_since_last_train=0)
        db.add(marker)
        db.flush()

    marker.recalibrations_since_last_train = (marker.recalibrations_since_last_train or 0) + 1
    db.flush()
    logger.info(
        "auto-retrain counter: recalibrations_since_last_train=%d (last_train_at=%s)",
        marker.recalibrations_since_last_train,
        marker.last_train_at.isoformat() if marker.last_train_at else "never",
    )
    return marker.recalibrations_since_last_train


def _percentile(values: List[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile (deterministic, no numpy) — sz-tcp.14 records the
    measured p95s alongside the audit event. None when there is no sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def _audit_cost(db: Session, event_name: str, node_def, details: dict) -> None:
    """Append-only AuditEvent for a tiered-cost recompute (sz-tcp.14, ISO 27001
    AU-2: no silent cost change). Never blocks the pricing math on an audit
    failure (matches services/admin_plans_pricing_service.py:_audit).

    tenant_id is the node's tenant; system nodes (tenant_id IS NULL) are
    attributed to the platform audit tenant so the row is never tenant-less.
    """
    try:
        import models
        from audit import PLATFORM_AUDIT_TENANT_ID

        db.add(models.AuditEvent(
            tenant_id=getattr(node_def, "tenant_id", None) or PLATFORM_AUDIT_TENANT_ID,
            user_id=None,
            event_name=event_name,
            details=details,
        ))
        db.flush()
    except Exception as e:  # noqa: BLE001
        logger.warning("cost audit (%s) failed for node %s: %s", event_name,
                       getattr(node_def, "id", "?"), e)


def _auto_calculate_credit_cost(node_def, db: Session):
    """Auto-calculate cost_in_credits from calibration resource samples.

    Formula: base(1) + GPU cost + CPU cost + memory cost
    Rates tuned so Sandbox (500 credits/month) gets ~50-100 workflow runs.

    Tier-aware only once the tier columns exist in the DB (they are deferred
    ORM columns that may be missing from a live DB pre-migration; touching
    them there errors). With tier columns: nodes whose cost_basis is 'free'
    or 'manual' are never recalibrated, others go through apply_node_cost
    (tiers follow, basis -> 'calibrated'). Without them nothing is written
    and no tier attribute is ever accessed.
    """
    from models import ApiTaskStatus
    from utils.node_cost import apply_node_cost, tier_columns_available

    import os
    GPU_RATE = float(os.getenv("GPU_CREDIT_RATE", "0.5"))   # credits per GPU-minute at 100% util
    CPU_RATE = float(os.getenv("CPU_CREDIT_RATE", "0.1"))   # credits per CPU-minute at 100% util
    MEM_RATE = float(os.getenv("MEM_CREDIT_RATE", "0.05"))  # credits per GB-minute

    # Before the tiered-pricing migration the cost basis is unreadable, so a
    # manual/free price can't be told apart from an estimate: write nothing.
    if not tier_columns_available(db):
        logger.info(
            "Skipping auto-calibration for node %d (%s): tier columns not migrated",
            node_def.id, node_def.name,
        )
        return
    if node_def.cost_basis in ("free", "manual"):
        logger.info(
            "Skipping auto-calibration for node %d (%s): cost_basis=%s",
            node_def.id, node_def.name, node_def.cost_basis,
        )
        _audit_cost(db, "cost.recompute_skipped", node_def, {
            "node_definition_id": node_def.id,
            "reason": f"cost_basis={node_def.cost_basis}",
            "cost_basis": node_def.cost_basis,
            "candidate_cost_in_credits": node_def.cost_in_credits,
            "candidate_cost_credits_by_tier": dict(node_def.cost_credits_by_tier or {}),
        })
        return

    try:
        calibration_tasks = db.query(ApiTaskQueue).filter(
            ApiTaskQueue.task_payload["node_definition_id"].as_string() == str(node_def.id),
            ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
            ApiTaskQueue.status == ApiTaskStatus.COMPLETED,
        ).all()

        if not calibration_tasks:
            logger.info("No completed calibration tasks for node %d, keeping default cost", node_def.id)
            _audit_cost(db, "cost.recompute_skipped", node_def, {
                "node_definition_id": node_def.id,
                "reason": "insufficient_samples",
                "cost_basis": node_def.cost_basis,
                "candidate_cost_in_credits": node_def.cost_in_credits,
                "candidate_cost_credits_by_tier": dict(node_def.cost_credits_by_tier or {}),
                "sample_count": 0,
                "calibration_task_count": 0,
            })
            return

        total_duration_s = 0
        total_gpu_pct = 0
        total_cpu_pct = 0
        total_ram_mb = 0
        sample_count = 0
        # sz-tcp.14: the same COMPLETED-only samples, kept per-sample so the
        # audit event can record the measured p95s. Purely observational — the
        # pricing math below still uses the averages, unchanged.
        gpu_pcts: List[float] = []
        cpu_pcts: List[float] = []
        ram_mbs: List[float] = []
        durations_min: List[float] = []

        for task in calibration_tasks:
            samples = db.query(TaskResourceSample).filter(
                TaskResourceSample.task_id == task.id
            ).all()
            for s in samples:
                total_gpu_pct += (s.gpu_util_pct or 0)
                total_cpu_pct += (s.cpu_percent or 0)
                total_ram_mb += (s.ram_mb or 0)
                sample_count += 1
                gpu_pcts.append(float(s.gpu_util_pct or 0))
                cpu_pcts.append(float(s.cpu_percent or 0))
                ram_mbs.append(float(s.ram_mb or 0))

            if task.started_at and task.completed_at:
                total_duration_s += (task.completed_at - task.started_at).total_seconds()
                durations_min.append(
                    (task.completed_at - task.started_at).total_seconds() / 60.0
                )

        if sample_count == 0 or total_duration_s == 0:
            _audit_cost(db, "cost.recompute_skipped", node_def, {
                "node_definition_id": node_def.id,
                "reason": "insufficient_samples",
                "cost_basis": node_def.cost_basis,
                "candidate_cost_in_credits": node_def.cost_in_credits,
                "candidate_cost_credits_by_tier": dict(node_def.cost_credits_by_tier or {}),
                "sample_count": sample_count,
                "calibration_task_count": len(calibration_tasks),
            })
            return

        avg_duration_min = (total_duration_s / len(calibration_tasks)) / 60
        avg_gpu_pct = total_gpu_pct / sample_count / 100
        avg_cpu_pct = total_cpu_pct / sample_count / 100
        avg_ram_gb = total_ram_mb / sample_count / 1024

        # OWNER DECISION 10-10 / sz-tcp.20 — the rate-conversion math itself
        # (base 1 + GPU/CPU/mem-minute cost) now lives in
        # utils.workflow_credits.credit_amount_from_resource_profile, the
        # single source of truth shared with compute_workflow_quote's exact
        # per-node pricing, so the two can never drift apart. This call site
        # keeps its own rounding policy (nearest, not up) — unchanged math.
        from utils.workflow_credits import credit_amount_from_resource_profile
        raw_amount = credit_amount_from_resource_profile(
            avg_duration_min, avg_gpu_pct, avg_cpu_pct, avg_ram_gb
        )
        calculated_cost = max(1, round(raw_amount))

        old_cost = node_def.cost_in_credits
        old_tiers = dict(node_def.cost_credits_by_tier or {})
        old_basis = node_def.cost_basis

        apply_node_cost(node_def, calculated_cost, basis="calibrated")

        _audit_cost(db, "cost.recompute", node_def, {
            "node_definition_id": node_def.id,
            "reason": "calibration_complete",
            "cost_basis": node_def.cost_basis,
            "cost_basis_before": old_basis,
            "old_cost_in_credits": old_cost,
            "new_cost_in_credits": node_def.cost_in_credits,
            "old_cost_credits_by_tier": old_tiers,
            "new_cost_credits_by_tier": dict(node_def.cost_credits_by_tier or {}),
            "sample_count": sample_count,
            "calibration_task_count": len(calibration_tasks),
            "rates": {"gpu": GPU_RATE, "cpu": CPU_RATE, "mem": MEM_RATE},
            "p95": {
                "gpu_util_pct": _percentile(gpu_pcts, 0.95),
                "cpu_pct": _percentile(cpu_pcts, 0.95),
                "ram_mb": _percentile(ram_mbs, 0.95),
                "duration_min": _percentile(durations_min, 0.95),
            },
        })

        logger.info(
            "Auto-calibrated credit cost for node %d (%s): %d → %d credits "
            "(gpu=%.1f%% cpu=%.1f%% ram=%.1fGB dur=%.1fmin)",
            node_def.id, node_def.name, old_cost, calculated_cost,
            avg_gpu_pct * 100, avg_cpu_pct * 100, avg_ram_gb, avg_duration_min
        )
    except Exception as e:
        logger.warning("Failed to auto-calculate credit cost for node %d: %s", node_def.id, e)


def _extract_node_kind(task: ApiTaskQueue) -> Optional[str]:
    """Extract node_kind from the task payload."""
    if not task.task_payload:
        return None
    # Support both nested and flat payload formats
    node_config = task.task_payload.get("node_config", {})
    return (task.task_payload.get("node_kind")
            or node_config.get("node_kind")
            or node_config.get("process_dag_id"))


def _extract_input_size(task: ApiTaskQueue) -> Optional[float]:
    """Extract total input file size in MB from task payload."""
    if not task.task_payload:
        return None
    inputs = task.task_payload.get("inputs", [])
    total_bytes = sum(inp.get("file_size_bytes", 0) for inp in inputs if isinstance(inp, dict))
    return total_bytes / (1024 * 1024) if total_bytes > 0 else None


def _extract_output_size(task: ApiTaskQueue) -> Optional[float]:
    """Extract total output file size in MB from result payload."""
    if not task.result_payload:
        return None
    outputs = task.result_payload.get("outputs", [])
    total_bytes = sum(out.get("file_size_bytes", 0) for out in outputs if isinstance(out, dict))
    return total_bytes / (1024 * 1024) if total_bytes > 0 else None


def _record_contention(pattern, payload: dict, duration, peak_ram, peak_vram):
    """Accumulate a contended measurement under pattern_data["contention"].

    Kept out of the baseline aggregates on purpose — see update_pattern_from_task.
    Keyed by the sorted stressor set so "cpu-multi+io" is one series regardless
    of the order the stressors were listed in, and stored as a slowdown factor
    against the isolated baseline because the factor is what transfers: it stays
    meaningful when the same node is later measured on faster hardware, where
    the absolute seconds would not.
    """
    from sqlalchemy.orm.attributes import flag_modified

    kinds = payload.get("stressor_kinds") or []
    key = "+".join(sorted(kinds)) if kinds else "unknown"

    data = dict(pattern.pattern_data or {})
    contention = dict(data.get("contention") or {})
    entry = dict(contention.get(key) or {})

    n = int(entry.get("samples") or 0)
    if duration is not None:
        prev = entry.get("avg_duration_seconds")
        entry["avg_duration_seconds"] = (
            duration if prev is None else (prev * n + duration) / (n + 1)
        )
    for field, value in (("avg_peak_ram_mb", peak_ram),
                         ("avg_peak_vram_mb", peak_vram)):
        if value is None:
            continue
        prev = entry.get(field)
        entry[field] = value if prev is None else (prev * n + value) / (n + 1)

    entry["samples"] = n + 1
    entry["run_kind"] = payload.get("calibration_run_kind")

    baseline = pattern.avg_duration_seconds
    if baseline and entry.get("avg_duration_seconds"):
        entry["slowdown_factor"] = round(
            entry["avg_duration_seconds"] / baseline, 3)

    contention[key] = entry
    data["contention"] = contention
    pattern.pattern_data = data
    flag_modified(pattern, "pattern_data")
