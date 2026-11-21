"""
Queue Packer — batch-optimizes task placement across all workers.

Replaces the one-at-a-time greedy scheduler with a batch optimizer that:
1. Predicts all (task × worker) combinations in one ONNX batch call
2. Greedy assignment sorted by priority
3. 2-opt swap improvement to reduce total makespan
4. 30-second hard timeout with greedy-only fallback
"""

from product_names import themed_logger
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from scheduler.timeline_simulator import TimelineSimulator, PlannedTask, ResourceSnapshot

logger = themed_logger(__name__, 'scheduler')

TIMEOUT_SECONDS = 30

# Adaptive 2-opt budget (Bucket E §5). Below SKIP_BELOW_UTIL_PCT the
# packer skips 2-opt entirely — at low utilisation, greedy assignment
# is near-optimal and 2-opt finds nothing to swap. Above
# FULL_ABOVE_UTIL_PCT it uses the full TIMEOUT_SECONDS budget. Linear
# scale between the two thresholds, bounded at PACKER_2OPT_BUDGET_MIN_S.


def _compute_two_opt_budget_s(util_pct: float) -> float:
    """Map fleet utilisation (0..100) to a 2-opt budget in seconds.

    util < skip_below      → 0 (skip 2-opt)
    util >= full_above     → TIMEOUT_SECONDS (full budget)
    in between             → linear scale [BUDGET_MIN_S, TIMEOUT_SECONDS]
    """
    skip_below = float(_packer_env("PACKER_2OPT_SKIP_BELOW_UTIL_PCT", "30"))
    # scheduler-runtime-config-panel — full_above is now operator-tunable
    # via the Settings → AI Configuration → Scheduler sub-tab. Cache
    # falls back through env (PACKER_2OPT_FULL_ABOVE_UTIL_PCT) → default 70.
    from services import scheduler_runtime_cache
    full_above = float(scheduler_runtime_cache.get_packer_full_above_util_pct())
    budget_min = float(_packer_env("PACKER_2OPT_BUDGET_MIN_S", "5"))

    # Defensive: if env vars are misconfigured (full_above <= skip_below),
    # fall through to full budget to preserve the prior fixed-budget
    # behaviour rather than silently dropping 2-opt. Checked first so a
    # bad config can't lurk behind the skip_below early-return.
    if full_above <= skip_below:
        return float(TIMEOUT_SECONDS)

    if util_pct < skip_below:
        return 0.0
    if util_pct >= full_above:
        return float(TIMEOUT_SECONDS)
    span = full_above - skip_below
    fraction = (util_pct - skip_below) / span
    return budget_min + fraction * (TIMEOUT_SECONDS - budget_min)


def _packer_env(name: str, default: str) -> str:
    """Read a packer tuning env var with default. Hot path — kept ultra-light."""
    return os.environ.get(name, default)


@dataclass
class PackingResult:
    assignments: Dict[int, 'WorkerAssignment']
    total_makespan_s: float
    tasks_placed: int
    tasks_unplaced: int
    optimization_time_ms: float
    used_2opt: bool


@dataclass
class WorkerAssignment:
    worker_name: str
    start_after_s: float
    planned_task: PlannedTask
    confidence: float
    complementary_score: float


class QueuePacker:
    """Batch optimizer for task-to-worker assignment."""

    def __init__(self, onnx_predictor=None):
        self.predictor = onnx_predictor

    def pack(
        self,
        pending_tasks: list,
        worker_timelines: Dict[str, TimelineSimulator],
        predictions: Dict[Tuple[int, str], 'PredictionResult'] = None,
        worker_metadata: Optional[Dict[str, Dict]] = None,
        db=None,
    ) -> PackingResult:
        """Pack pending tasks onto worker timelines.

        Priority-class branch: tasks tagged `priority_class='interactive'`
        skip the 2-opt search entirely and use the lightweight warmth-score
        placement (target <50ms per task). All other priority classes run
        the existing greedy + 2-opt pipeline.

        Bucket H-2 additions:
          - Failure-aware retry placement: tasks with `attempt_count > 1`
            and `prior_workers` populated exclude those workers from their
            candidate set. Defends against retry loops on a single broken
            box where the same worker keeps claiming and failing the task.
          - Worker health soft-penalty: per-worker `health_penalty_s`
            (computed from recent disconnect/OOM/GPU-error history) is
            added to the placement score as an additional makespan term.
            Soft (additive penalty) not hard exclusion so transient blips
            don't permanently down-weight workers.

        worker_metadata (optional, per-worker): {
            worker_name: {
                "cached_images": [...],
                "model_warmth_score": float,
                "current_load_pct": float,
                "hardware_tags": [...],
                "health_penalty_s": float,    # H-2 — added to score
            }
        }
        task_info dict for retry-aware placement: {
            "task_id": int,
            "node_kind": str,
            "priority": int,
            "priority_class": str,
            "attempt_count": int,             # H-2 — defaults to 0
            "prior_workers": [str, ...],      # H-2 — workers that ran prior attempts
        }
        """
        start_time = time.monotonic()

        if not pending_tasks or not worker_timelines:
            return PackingResult({}, 0, 0, len(pending_tasks), 0, False)

        # Carve interactive tasks off the front; they go through the fast path.
        interactive_tasks = [t for t in pending_tasks if t.get("priority_class") == "interactive"]
        non_interactive_tasks = [t for t in pending_tasks if t.get("priority_class") != "interactive"]

        assignments: Dict[int, WorkerAssignment] = {}
        sim_states = {name: sim for name, sim in worker_timelines.items()}

        if interactive_tasks:
            self._lightweight_interactive_place(
                interactive_tasks,
                sim_states,
                predictions,
                worker_metadata or {},
                assignments,
            )

        if not non_interactive_tasks:
            total_makespan = max((s.makespan() for s in sim_states.values()), default=0.0)
            elapsed_ms = (time.monotonic() - start_time) * 1000
            logger.info(
                "Queue packing (interactive-only): %d tasks placed, makespan=%.1fs, time=%.0fms",
                len(assignments), total_makespan, elapsed_ms,
            )
            return PackingResult(
                assignments=assignments,
                total_makespan_s=total_makespan,
                tasks_placed=len(assignments),
                tasks_unplaced=len(pending_tasks) - len(assignments),
                optimization_time_ms=elapsed_ms,
                used_2opt=False,
            )

        sorted_tasks = sorted(non_interactive_tasks, key=lambda t: (-t.get("priority", 0), t.get("task_id", 0)))

        for task_info in sorted_tasks:
            if time.monotonic() - start_time > TIMEOUT_SECONDS:
                logger.warning("Queue packing timeout — placing remaining tasks greedily")
                break

            task_id = task_info.get("task_id")
            best_worker = None
            best_score = float('inf')
            best_start = 0.0
            best_planned = None
            best_confidence = 0.0

            # H-2 — exclude prior workers from retry candidates.
            attempt_count = int(task_info.get("attempt_count", 0))
            prior_workers = set(task_info.get("prior_workers") or [])

            # Deferred-5 — start_time staggering as an explicit candidate axis.
            # Instead of always placing the task at the worker's current
            # makespan, we evaluate K candidate start times [earliest +
            # offset_pct * predicted_duration for offset_pct in OFFSETS].
            # This lets the scorer pick start times that align the focal
            # task's high-resource phase with concurrent tasks' low-
            # resource phase — a real makespan win when phase profiles
            # otherwise overlap. Offsets configurable via env; default
            # [0, 0.25, 0.5] gives 3 candidates per (task, worker).
            offsets_env = _packer_env("PACKER_STAGGER_OFFSETS", "0,0.25,0.5")
            try:
                stagger_offsets = [float(x) for x in offsets_env.split(",") if x.strip()]
            except ValueError:
                stagger_offsets = [0.0]
            if not stagger_offsets:
                stagger_offsets = [0.0]

            for worker_name, sim in sim_states.items():
                # H-2 retry placement: first retry → never the worker that
                # just failed; second+ retries → exclude all priors so
                # we explore. Hard exclusion (skip) not soft penalty
                # because a worker that just OOMed on this exact task
                # is going to OOM on it again.
                if attempt_count > 0 and worker_name in prior_workers:
                    continue

                pred_key = (task_id, worker_name)
                pred = predictions.get(pred_key) if predictions else None

                if pred is None:
                    continue

                # Pre-compute the earliest feasible start once per (task,
                # worker) — same fit check the original logic used.
                base_planned = PlannedTask(
                    task_id=task_id,
                    node_kind=task_info.get("node_kind", ""),
                    start_offset_s=sim.makespan(),
                    processing_duration_s=pred.processing_duration_s,
                    upload_duration_s=pred.upload_duration_s,
                    download_duration_s=pred.download_duration_s,
                    peak_ram_mb=pred.peak_ram_mb,
                    peak_vram_mb=pred.peak_vram_mb,
                    peak_cpu_pct=pred.peak_cpu_pct,
                    peak_net_mbps=pred.peak_net_mbps,
                    pattern_type=pred.pattern_type,
                    is_gpu_bound=pred.peak_vram_mb > 0 and pred.pattern_type in ("plateau", "step_function"),
                )
                fits, earliest = sim.can_fit_task(base_planned, sim.makespan())
                base_start = sim.makespan() if fits else earliest

                # Evaluate at every staggered start time and keep the
                # best. Per-iteration cost is one simulate_task + one
                # complementary_score; cheap relative to the predict
                # call which already happened above.
                for offset_pct in stagger_offsets:
                    candidate_start = base_start + offset_pct * pred.processing_duration_s
                    planned = PlannedTask(
                        task_id=task_id,
                        node_kind=task_info.get("node_kind", ""),
                        start_offset_s=candidate_start,
                        processing_duration_s=pred.processing_duration_s,
                        upload_duration_s=pred.upload_duration_s,
                        download_duration_s=pred.download_duration_s,
                        peak_ram_mb=pred.peak_ram_mb,
                        peak_vram_mb=pred.peak_vram_mb,
                        peak_cpu_pct=pred.peak_cpu_pct,
                        peak_net_mbps=pred.peak_net_mbps,
                        pattern_type=pred.pattern_type,
                        is_gpu_bound=pred.peak_vram_mb > 0 and pred.pattern_type in ("plateau", "step_function"),
                    )

                    new_sim = sim.simulate_task(planned)
                    new_makespan = new_sim.makespan()
                    comp_score = sim.complementary_score(planned)

                    # Bucket H-1 score function (now evaluated per (worker,
                    # start_time) candidate so the staggering search picks
                    # the start that minimises total cost):
                    #
                    #   score = α·makespan
                    #         + β·priority_lateness
                    #         + γ·variance_penalty
                    #         + ε·image_pull_cost
                    #         + health_penalty
                    #         − ζ·model_warmth_bonus
                    #         − complementary_score
                    meta = (worker_metadata or {}).get(worker_name, {})
                    images = meta.get("cached_images") or []
                    node_kind_value = task_info.get("node_kind", "")
                    image_uncached_penalty = 0.0 if (node_kind_value and any(node_kind_value in img for img in images)) else float(_packer_env("PACKER_IMAGE_PULL_PENALTY_S", "30"))
                    model_warmth_bonus = float(meta.get("model_warmth_score", 0.0)) * float(_packer_env("PACKER_MODEL_WARMTH_BONUS_S", "20"))
                    # scheduler-runtime-config-panel — variance penalty
                    # is operator-tunable via the Scheduler sub-tab.
                    # Cache falls back through env (PACKER_VARIANCE_PENALTY_S)
                    # → default 15s.
                    from services import scheduler_runtime_cache
                    variance_penalty = (1.0 - float(pred.pattern_confidence or 0.5)) * float(scheduler_runtime_cache.get_variance_penalty_s())
                    pri = max(0, int(task_info.get("priority", 0)))
                    priority_lateness = pri * (planned.start_offset_s or 0) * float(_packer_env("PACKER_PRIORITY_LATENESS_COEF", "0.1"))
                    health_penalty = float(meta.get("health_penalty_s", 0.0))

                    alpha = float(_packer_env("PACKER_ALPHA_MAKESPAN", "1.0"))
                    score = (
                        alpha * new_makespan
                        + priority_lateness
                        + variance_penalty
                        + image_uncached_penalty
                        + health_penalty
                        - model_warmth_bonus
                        - comp_score * 10
                    )

                    if score < best_score:
                        best_score = score
                        best_worker = worker_name
                        best_start = planned.start_offset_s
                        best_planned = planned
                        best_confidence = pred.pattern_confidence

            if best_worker and best_planned:
                assignments[task_id] = WorkerAssignment(
                    worker_name=best_worker,
                    start_after_s=best_start,
                    planned_task=best_planned,
                    confidence=best_confidence,
                    complementary_score=sim_states[best_worker].complementary_score(best_planned),
                )
                sim_states[best_worker] = sim_states[best_worker].simulate_task(best_planned)

        greedy_time = time.monotonic() - start_time
        used_2opt = False

        # Adaptive 2-opt budget — utilisation determines whether to run
        # the swap pass and for how long. Reuses the calibration
        # dispatcher's helper to avoid a second DB query.
        util_pct = 0.0
        two_opt_budget_s = float(TIMEOUT_SECONDS)  # default if no db
        if db is not None:
            try:
                from services.calibration_dispatcher import fleet_headroom_pct
                headroom = fleet_headroom_pct(db)
                util_pct = max(0.0, min(100.0, (1.0 - headroom) * 100.0))
                two_opt_budget_s = _compute_two_opt_budget_s(util_pct)
            except Exception as e:
                # Don't let a util-read failure break dispatch. Fall back
                # to the prior fixed-budget behaviour.
                logger.debug("util read failed, using full 2-opt budget: %s", e)

        if (
            len(assignments) > 2
            and two_opt_budget_s > 0
            and (time.monotonic() - start_time) < two_opt_budget_s - 1
        ):
            improved = self._two_opt_improve(
                assignments, sim_states, worker_timelines, predictions,
                start_time, two_opt_budget_s,
            )
            if improved:
                assignments = improved
                used_2opt = True

        total_makespan = max(
            (sim.makespan() for sim in sim_states.values()),
            default=0.0,
        )
        elapsed_ms = (time.monotonic() - start_time) * 1000

        logger.info(
            "Queue packing: %d/%d tasks placed, makespan=%.1fs, time=%.0fms, "
            "2opt=%s, 2opt_budget=%.1fs (util=%.0f%%)",
            len(assignments), len(pending_tasks), total_makespan, elapsed_ms,
            used_2opt, two_opt_budget_s, util_pct,
        )

        return PackingResult(
            assignments=assignments,
            total_makespan_s=total_makespan,
            tasks_placed=len(assignments),
            tasks_unplaced=len(pending_tasks) - len(assignments),
            optimization_time_ms=elapsed_ms,
            used_2opt=used_2opt,
        )

    def _lightweight_interactive_place(
        self,
        tasks: list,
        sim_states: Dict[str, TimelineSimulator],
        predictions: Dict,
        worker_metadata: Dict[str, Dict],
        assignments: Dict[int, WorkerAssignment],
    ) -> None:
        """Single-pass placement for `priority_class='interactive'` tasks.

        Skip the 2-opt search entirely; pick the worker with the highest
        warmth score (image cached, model recently warm, lower current
        load) and dispatch immediately. Targets <50ms per task. Mutates
        `assignments` and `sim_states` in place.

        Warmth score (per-worker, configurable via env):
            score = W1 * image_cached
                  + W2 * model_warmth
                  - W3 * current_load_pct

        Defaults W1=0.5, W2=0.3, W3=0.2.
        """
        import os as _os
        w1 = float(_os.getenv("PACKER_INTERACTIVE_W_IMAGE", "0.5"))
        w2 = float(_os.getenv("PACKER_INTERACTIVE_W_MODEL_WARMTH", "0.3"))
        w3 = float(_os.getenv("PACKER_INTERACTIVE_W_LOAD", "0.2"))

        for task_info in tasks:
            task_id = task_info.get("task_id")
            node_kind = task_info.get("node_kind", "")

            best_worker = None
            best_score = float("-inf")
            best_pred = None

            for worker_name, sim in sim_states.items():
                pred = predictions.get((task_id, worker_name)) if predictions else None
                if pred is None:
                    continue

                meta = worker_metadata.get(worker_name, {})
                images = meta.get("cached_images") or []
                image_hit = 1.0 if node_kind and any(node_kind in img for img in images) else 0.0
                model_warmth = float(meta.get("model_warmth_score", 0.0))
                load_pct = float(meta.get("current_load_pct", 0.0))

                score = w1 * image_hit + w2 * model_warmth - w3 * load_pct

                if score > best_score:
                    best_score = score
                    best_worker = worker_name
                    best_pred = pred

            if best_worker is None or best_pred is None:
                continue

            sim = sim_states[best_worker]
            planned = PlannedTask(
                task_id=task_id,
                node_kind=node_kind,
                start_offset_s=sim.makespan(),
                processing_duration_s=best_pred.processing_duration_s,
                upload_duration_s=best_pred.upload_duration_s,
                download_duration_s=best_pred.download_duration_s,
                peak_ram_mb=best_pred.peak_ram_mb,
                peak_vram_mb=best_pred.peak_vram_mb,
                peak_cpu_pct=best_pred.peak_cpu_pct,
                peak_net_mbps=best_pred.peak_net_mbps,
                pattern_type=best_pred.pattern_type,
                is_gpu_bound=best_pred.peak_vram_mb > 0 and best_pred.pattern_type in ("plateau", "step_function"),
            )

            fits, earliest = sim.can_fit_task(planned, sim.makespan())
            if not fits:
                planned.start_offset_s = earliest

            sim_states[best_worker] = sim.simulate_task(planned)
            assignments[task_id] = WorkerAssignment(
                worker_name=best_worker,
                start_after_s=planned.start_offset_s,
                planned_task=planned,
                confidence=best_pred.pattern_confidence,
                complementary_score=best_score,
            )

    def _two_opt_improve(
        self,
        assignments: Dict[int, WorkerAssignment],
        sim_states: Dict[str, TimelineSimulator],
        original_timelines: Dict[str, TimelineSimulator],
        predictions: Dict,
        start_time: float,
        budget_s: float = TIMEOUT_SECONDS,
    ) -> Optional[Dict[int, WorkerAssignment]]:
        task_ids = list(assignments.keys())
        improved = False
        best_assignments = dict(assignments)

        current_makespan = max((s.makespan() for s in sim_states.values()), default=0)

        # Stop ~1s before the budget runs out so we have time to return
        # cleanly without truncating mid-swap.
        deadline = start_time + max(0.0, budget_s - 1.0)
        for i in range(len(task_ids)):
            if time.monotonic() > deadline:
                break
            for j in range(i + 1, len(task_ids)):
                tid_a = task_ids[i]
                tid_b = task_ids[j]
                a = best_assignments[tid_a]
                b = best_assignments[tid_b]

                if a.worker_name == b.worker_name:
                    continue

                swapped = dict(best_assignments)
                swapped[tid_a] = WorkerAssignment(
                    worker_name=b.worker_name,
                    start_after_s=b.start_after_s,
                    planned_task=a.planned_task,
                    confidence=a.confidence,
                    complementary_score=a.complementary_score,
                )
                swapped[tid_b] = WorkerAssignment(
                    worker_name=a.worker_name,
                    start_after_s=a.start_after_s,
                    planned_task=b.planned_task,
                    confidence=b.confidence,
                    complementary_score=b.complementary_score,
                )

                test_sims = {name: TimelineSimulator(
                    s.total_ram_mb, s.total_vram_mb, s.cpu_cores, s.nic_capacity_mbps,
                    # MAC.1.s9b — carry the Metal worker's ONE pool across the
                    # 2-opt rebuild, otherwise the swap re-scores against
                    # phantom separate RAM/VRAM devices. getattr keeps older
                    # sim objects working unchanged.
                    shared_pool_mb=getattr(s, "shared_pool_mb", 0.0),
                ) for name, s in original_timelines.items()}

                for tid, assign in swapped.items():
                    task = assign.planned_task
                    test_sims[assign.worker_name] = test_sims[assign.worker_name].simulate_task(task)

                new_makespan = max((s.makespan() for s in test_sims.values()), default=0)

                if new_makespan < current_makespan * 0.99:
                    best_assignments = swapped
                    current_makespan = new_makespan
                    improved = True

        return best_assignments if improved else None
