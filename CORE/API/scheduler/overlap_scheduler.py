"""
Overlap Scheduler — the brain of the system.

Assigns tasks to specific workers with computed phase plans and start-after
timestamps based on multi-resource timeline analysis. Runs both synchronously
(on enqueue) and as a background loop (every 5 seconds).

All scheduling intelligence lives here. Workers are dumb executors.
"""

import json
from product_names import themed_logger
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from models import (
    ApiTaskQueue, ApiTaskStatus,
    HardwareProfile, ResourcePattern, TaskStatus, WorkflowRun,
    WorkerLicense, WorkerResourceTimeline, WorkerStatus,
)
from scheduler.contention_model import (
    MemoryPressure,
    docker_operation_delay,
    disk_throughput_factor,
    effective_nic_throughput,
    effective_vram,
    gc_pause_overhead_seconds,
    gpu_warmup_seconds,
    image_mount_seconds,
    remote_worker_overhead_seconds,
    thermal_cpu_factor,
)

logger = themed_logger(__name__, 'scheduler')

# Feature flag
PREDICTIVE_SCHEDULING_ENABLED = os.getenv("PREDICTIVE_SCHEDULING_ENABLED", "false").lower() == "true"

# Opt-in batch packing: on every scheduling tick, collect the whole
# PENDING queue, build per-worker timelines, batch-optimise placements
# via QueuePacker (2-opt makespan reduction), and only fall back to
# per-task greedy for tasks the packer couldn't place. Default on when
# PREDICTIVE_SCHEDULING_ENABLED — batch packing's fallback reduces to
# the same per-task path, so enabling it is strictly a Pareto improvement
# unless the packer itself misbehaves. Set BATCH_PACKING_ENABLED=false
# to opt out and revert to pure per-task greedy.
BATCH_PACKING_ENABLED = os.getenv(
    "BATCH_PACKING_ENABLED",
    "true" if PREDICTIVE_SCHEDULING_ENABLED else "false"
).lower() == "true"

# Background loop interval
SCHEDULING_LOOP_INTERVAL = 5.0  # seconds

# Assignment expiry
ASSIGNMENT_EXPIRY_MINUTES = 5

# Profiling (hardware benchmark) wall-clock deadline. A profiling WorkflowRun
# that has been in QUEUED/RUNNING state longer than this gets force-failed
# by _expire_stuck_profiling_runs. Keeps the UI from sitting on
# "Queued — waiting for available worker" forever when image pulls, worker
# crashes, or config issues stall the benchmark.
PROFILING_MAX_RUNTIME_MINUTES = int(os.getenv("PROFILING_MAX_RUNTIME_MINUTES", "10"))
STALE_ASSIGNMENT_MINUTES = 10

# Missed heartbeats before considering worker offline
OFFLINE_HEARTBEAT_THRESHOLD = 3  # 3 × 15s = 45s


# FX.115-adjacent (observed live 2026-10-09, build run 2241 / task 50645):
# a task on a non-GPU queue was handed a phase plan with vram_mb=39542, so
# every worker's validate-phase check failed ("VRAM: need 39542MB contiguous,
# largest block is 0MB"), which reverts the task to PENDING + bumps
# scheduling_version.  The claim/validate/revert loop ran ~1.4x/second
# (scheduling_version 1172 -> 1200 in 20s) and the task never STARTED.
#
# The queue is the authority on whether a task needs VRAM at all: cpu/npu
# variants run with no GPU, so their VRAM demand is 0 by definition, whatever
# the historical pattern says.  Patterns are keyed (node_kind, queue_name,
# size_tier) and the queue-wide fallback in _get_pattern() step 3 can hand a
# GPU node's profile to a CPU task, so a stale/foreign avg_peak_vram_mb is
# reachable on a CPU row even after the writer guard below zeroes new writes.
def _gpu_queue_names() -> frozenset:
    """Queue names whose tasks are allowed to demand VRAM."""
    return frozenset({"cuda", "rocm", "intel", "npu", "accelerator", "gpu"})


def _vram_demand_for_queue(task: ApiTaskQueue, vram_mb) -> float:
    """Clamp a task's predicted VRAM demand to 0 on non-GPU queues."""
    if not vram_mb:
        return 0.0
    queue = (getattr(task, "queue_name", None) or "").strip().lower()
    if queue in _gpu_queue_names():
        return float(vram_mb)
    return 0.0


def _advanced_scheduling_allowed(db: Optional[Session] = None) -> bool:
    """open-core-tier-structure gate: Cap.ADVANCED_SCHEDULING.

    Without this entitlement, neither the learning-based single-task
    placement (schedule_task) nor the batch/overlap packing background
    loop may run — the deployment must fall back to plain FIFO claiming
    in routers/worker_tasks.py's _try_claim_task (oldest PENDING task
    first). PREDICTIVE_SCHEDULING_ENABLED / BATCH_PACKING_ENABLED stay as
    the operator-facing on/off switches; this is the license/tier switch
    layered on top of them — both must agree for the advanced path to run.

    Self-host resolves this deployment-wide (db is ignored by
    get_entitlements there), so `db=None` is valid for the background-loop
    call site, which has no per-task session to hand in.

    Fails CLOSED: any error resolving entitlements is treated as "not
    granted" so a transient license-resolution failure degrades to safe
    FIFO rather than silently unlocking the paid tier.
    """
    try:
        from utils.entitlements import get_entitlements, evaluate_feature, Cap
        es = get_entitlements(db)  # self-host: deployment-wide
        return evaluate_feature(Cap.ADVANCED_SCHEDULING, es).allowed
    except Exception:
        logger.exception(
            "advanced_scheduling entitlement check failed; defaulting to FIFO"
        )
        return False


class Placement:
    """Result of placing a task on a worker."""

    def __init__(self, worker_name: str, start_after: datetime,
                 phase_plan: dict, confidence: float, reasoning: str,
                 completion_estimate: datetime):
        self.worker_name = worker_name
        self.start_after = start_after
        self.phase_plan = phase_plan
        self.confidence = confidence
        self.reasoning = reasoning
        self.completion_estimate = completion_estimate


def _is_unified_memory_worker(worker) -> bool:
    """MAC.1.s9a — is this worker an Apple-Silicon (Metal) box with ONE
    shared RAM ∪ VRAM pool?

    Mirrors the Metal-lane detection in routers/worker_tasks.py
    (`worker_metal_lane`): the reported OS is `darwin`, OR the worker carries
    the `metal` hardware tag (the tag MAC.1.s7's Go probe emits, paired with
    the unified-memory figure). The tag alone is enough — a linux box tagged
    `metal` is an Apple-Box-in-the-Cloud edge and shares the same pool.

    Discrete-GPU (CUDA/ROCm) workers report neither, so every existing
    two-device budget path stays byte-for-byte identical.
    """
    hw = getattr(worker, "hardware_specs", None) or {}
    worker_os = str(hw.get("os") or "").strip().lower()
    if worker_os == "darwin":
        return True
    tags = hw.get("hardware_tags") or []
    if not isinstance(tags, list):
        return False
    return any(str(t).strip().lower() == "metal" for t in tags if isinstance(t, str))


class MultiResourceTimeline:
    """
    Projected resource availability over time for a single worker.

    Each resource dimension is a list of (time_offset_seconds, value) tuples
    representing projected AVAILABLE capacity at that time.
    """

    def __init__(self, worker: WorkerLicense, snapshot: Optional[WorkerResourceTimeline]):
        self.worker = worker
        self.snapshot = snapshot

        hw = worker.hardware_specs or {}
        self.ram_total_mb = hw.get("ram_gb", 0) * 1024
        self.cpu_cores = hw.get("cpu_cores", 1)
        self.vram_total_mb = hw.get("vram_gb", 0) * 1024
        self.nic_capacity_mbps = 1000.0  # Default 1Gbps
        self.gpu_type = hw.get("gpu_type", "none")

        if snapshot:
            self.nic_capacity_mbps = snapshot.nic_capacity_mbps or 1000.0

        # RAM ceiling: 85% to avoid kernel pressure
        self.ram_ceiling_mb = MemoryPressure.effective_ram_ceiling(self.ram_total_mb)

        # MAC.1.s9a — unified memory (Apple Silicon / Metal lane). Such a box
        # has ONE RAM ∪ VRAM pool: the probe reports the unified figure in
        # both fields (or leaves vram_gb absent), so the pool is the LARGER
        # of the two, never their sum. Discrete-GPU workers keep
        # unified_memory False and every number below stays untouched, so
        # CUDA/ROCm budgeting is byte-for-byte the old behaviour.
        self.unified_memory = _is_unified_memory_worker(worker)
        if self.unified_memory:
            self.shared_pool_mb = max(self.ram_total_mb, self.vram_total_mb)
            self.shared_ceiling_mb = MemoryPressure.effective_ram_ceiling(
                self.shared_pool_mb
            )
        else:
            self.shared_pool_mb = 0.0
            self.shared_ceiling_mb = 0.0

        # Current state from snapshot
        self.current_ram_available_mb = self.ram_ceiling_mb
        self.current_cpu_available = float(self.cpu_cores)
        self.current_vram_available_mb = float(self.vram_total_mb)
        self.current_nic_rx_mbps = 0.0
        self.current_nic_tx_mbps = 0.0
        self.current_docker_ops = 0
        self.current_gpu_power_state = "active"
        self.current_gpu_temp_c = 0.0
        self.current_cpu_temp_c = 0.0
        self.thermal_throttled = False

        # MAC.1.s9a — shared-pool bookkeeping for Metal workers. One pool, so
        # one availability number: it starts at the pool ceiling and every
        # placement (or running-task projection) deducts RAM+VRAM from it
        # ONCE. Discrete-GPU workers keep this at zero and never read it.
        self.current_shared_available_mb = (
            self.shared_ceiling_mb if self.unified_memory else 0.0
        )

        if snapshot:
            if snapshot.ram_available_mb is not None:
                self.current_ram_available_mb = min(snapshot.ram_available_mb, self.ram_ceiling_mb)
                if self.unified_memory:
                    # MAC.1.s9a — on a Metal box the probe's `ram_available_mb`
                    # IS the free half of the single pool (MAC.1.s7 reports the
                    # unified figure), so it seeds the shared number directly.
                    self.current_shared_available_mb = min(
                        snapshot.ram_available_mb, self.shared_ceiling_mb
                    )
            if snapshot.cpu_percent is not None:
                self.current_cpu_available = self.cpu_cores * (1.0 - snapshot.cpu_percent / 100.0)
            if snapshot.vram_used_mb is not None and self.vram_total_mb > 0:
                frag = snapshot.vram_fragmentation_pct or 0
                concurrent_gpu = len([
                    t for t in (snapshot.task_snapshots or {}).values()
                    if isinstance(t, dict) and (t.get("vram_mb") or 0) > 0
                ])
                self.current_vram_available_mb = effective_vram(
                    self.vram_total_mb - (snapshot.vram_used_mb or 0),
                    concurrent_gpu, frag
                )
            self.current_nic_rx_mbps = snapshot.nic_rx_mbps or 0
            self.current_nic_tx_mbps = snapshot.nic_tx_mbps or 0
            self.current_docker_ops = snapshot.containers_starting or 0
            self.current_gpu_power_state = snapshot.gpu_power_state or "active"
            self.current_gpu_temp_c = snapshot.gpu_temperature_c or 0
            self.current_cpu_temp_c = snapshot.cpu_temperature_c or 0
            self.thermal_throttled = snapshot.thermal_throttled or False

        # Running task projections will be added here
        self.running_tasks_ram_freed = []  # [(time_offset_s, ram_mb_freed)]

    def can_fit_now(self, ram_mb: float, vram_mb: float = 0, cpu_cores: float = 0) -> bool:
        """Quick check: can this resource demand fit right now?

        MAC.1.s9a — a Metal (unified-memory) worker has ONE RAM ∪ VRAM pool,
        so its combined demand is checked ONCE against what is left of that
        pool. The discrete-GPU branch below is untouched: CUDA/ROCm workers
        still see RAM and VRAM as two separate devices.
        """
        if self.unified_memory:
            if (ram_mb + vram_mb) > self.current_shared_available_mb:
                return False
            if cpu_cores > 0 and cpu_cores > self.current_cpu_available:
                return False
            return True
        if ram_mb > self.current_ram_available_mb:
            return False
        if vram_mb > 0 and vram_mb > self.current_vram_available_mb:
            return False
        if cpu_cores > 0 and cpu_cores > self.current_cpu_available:
            return False
        return True

    def reserve(self, ram_mb: float, vram_mb: float = 0, cpu_cores: float = 0) -> None:
        """MAC.1.s9a — deduct a placed task's demand from this projection.

        Unified-memory workers: RAM+VRAM counted once against the shared
        pool. Discrete workers: the two device pools separately (callers on
        the CUDA path never relied on this before; behaviour of the existing
        placement code is unchanged either way).
        """
        if self.unified_memory:
            self.current_shared_available_mb = max(
                0.0, self.current_shared_available_mb - (ram_mb + vram_mb)
            )
            if cpu_cores > 0:
                self.current_cpu_available = max(0.0, self.current_cpu_available - cpu_cores)
            return
        self.current_ram_available_mb = max(0.0, self.current_ram_available_mb - ram_mb)
        if vram_mb > 0:
            self.current_vram_available_mb = max(0.0, self.current_vram_available_mb - vram_mb)
        if cpu_cores > 0:
            self.current_cpu_available = max(0.0, self.current_cpu_available - cpu_cores)

    def is_thermally_throttled(self) -> bool:
        return self.thermal_throttled

    def is_in_red_memory_zone(self) -> bool:
        if self.unified_memory:
            # MAC.1.s9a — pressure is measured against the ONE pool.
            used = self.shared_pool_mb - self.current_shared_available_mb
            return MemoryPressure.classify(used, self.shared_pool_mb) == MemoryPressure.RED
        used = self.ram_total_mb - self.current_ram_available_mb
        return MemoryPressure.classify(used, self.ram_total_mb) == MemoryPressure.RED


class OverlapScheduler:
    """
    Core scheduler that assigns tasks to workers with computed phase plans.

    Runs in two modes:
    1. Synchronous: called on enqueue for immediate placement
    2. Background loop: processes PENDING tasks every 5 seconds
    """

    def __init__(self, db_factory):
        """
        Args:
            db_factory: Callable that returns a new SQLAlchemy Session
        """
        self.db_factory = db_factory
        self._stop_event = threading.Event()
        self._thread = None

        # In-memory cache of worker snapshots (updated by heartbeats)
        self._snapshot_cache: Dict[str, WorkerResourceTimeline] = {}
        self._cache_lock = threading.Lock()

    def update_snapshot_cache(self, worker_name: str, snapshot: WorkerResourceTimeline):
        """Called from heartbeat handler to keep cache fresh."""
        with self._cache_lock:
            self._snapshot_cache[worker_name] = snapshot

    def schedule_task(self, task: ApiTaskQueue, db: Session) -> bool:
        """
        Try to find a placement for a task. Returns True if placed.

        Called synchronously from the enqueue endpoint.
        """
        if not PREDICTIVE_SCHEDULING_ENABLED:
            return False
        if not _advanced_scheduling_allowed(db):
            return False
        # GX.D1 — a placement-pinned task is never pre-placed by score: it
        # stays PENDING so the matching worker's own fallback claim (which
        # enforces the pin at the SQL layer) is the placement decision.
        from services.workflow_placement import _is_placement_pinned
        if _is_placement_pinned(task.task_payload):
            return False

        # Get the resource pattern for this task's node_kind
        node_kind = self._extract_node_kind(task)
        pattern = self._get_pattern(node_kind, task.queue_name, db)

        # Get eligible workers
        workers = self._get_eligible_workers(task, db)
        # Durable pin: a task pinned to a specific worker (benchmark or
        # host-pinned stack) may only be placed on that worker — never score
        # alternatives into the placement decision.
        from utils.task_pinning import durable_target_worker
        pin = durable_target_worker(task)
        if pin:
            workers = [w for w in workers if w.worker_name == pin]
        if not workers:
            logger.debug("No eligible workers for task %d", task.id)
            return False

        # Build timeline and find placement for each worker
        placements = []
        for worker in workers:
            snapshot = self._get_snapshot(worker.worker_name, db)
            timeline = MultiResourceTimeline(worker, snapshot)

            # Skip workers in bad states
            if timeline.is_thermally_throttled():
                logger.debug("Skipping %s: thermal throttled", worker.worker_name)
                continue
            if timeline.is_in_red_memory_zone():
                logger.debug("Skipping %s: red memory zone", worker.worker_name)
                continue

            placement = self._find_placement(task, worker, timeline, pattern, db)
            if placement:
                placements.append(placement)

        if not placements:
            logger.debug("No valid placement found for task %d", task.id)
            return False

        # Select best placement
        best = self._select_best(task, placements, db)

        # Assign task — but ONLY if the task is still PENDING. A fast
        # worker (embedded worker on the same docker network) can claim
        # the row while we're still running placement (~10–50ms): the
        # original task INSERT used status=PENDING so the row is
        # visible to /poll-task's fallback PENDING claim path well
        # before our placement finishes. Without this re-check we'd
        # clobber CLAIMED/RUNNING back to ASSIGNED, the worker would
        # be told via Redis pub/sub that a task is available, a second
        # poll goroutine would claim it legitimately, and we'd end up
        # with two containers running the same task in parallel
        # (observed on run 7 task 497 / push-intel — see commit log).
        #
        # Refresh + recheck inside an atomic UPDATE…WHERE status=PENDING
        # so a concurrent claim that lands between our re-check and the
        # write still loses cleanly: we update zero rows and bail.
        from sqlalchemy import text as _sql_text
        result = db.execute(
            _sql_text("""
                UPDATE api_task_queue
                SET status = :assigned,
                    assigned_to_worker = :worker,
                    start_after = :start_after,
                    phase_plan = :phase_plan,
                    scheduling_metadata = :scheduling_metadata
                WHERE id = :task_id
                  AND status = :pending
                RETURNING id
            """),
            {
                "assigned": ApiTaskStatus.ASSIGNED.name,
                "pending": ApiTaskStatus.PENDING.name,
                "worker": best.worker_name,
                "start_after": best.start_after,
                "phase_plan": json.dumps(best.phase_plan) if best.phase_plan else None,
                "scheduling_metadata": json.dumps({
                    "confidence": best.confidence,
                    "reasoning": best.reasoning,
                    "placed_at": datetime.now(timezone.utc).isoformat(),
                    "completion_estimate": best.completion_estimate.isoformat(),
                }),
                "task_id": task.id,
            },
        )
        row = result.fetchone()
        db.commit()
        if row is None:
            # A worker claimed it between our PENDING read and this
            # UPDATE. That's fine — the worker is now running it; the
            # placement work we just did is wasted but the system is
            # consistent. Skip the assigned-to-worker log and the
            # Redis publish caller; nothing else to do here.
            logger.info(
                "Task %d skipped placement: row no longer PENDING "
                "(worker claimed it during placement).", task.id,
            )
            return False

        logger.info(
            "Task %d assigned to %s: start_after=%s confidence=%.2f",
            task.id, best.worker_name, best.start_after.isoformat(), best.confidence,
        )
        return True

    def _find_placement(self, task: ApiTaskQueue, worker: WorkerLicense,
                        timeline: MultiResourceTimeline,
                        pattern: Optional[ResourcePattern],
                        db: Session) -> Optional[Placement]:
        """Find the best placement for a task on a specific worker."""
        now = datetime.now(timezone.utc)
        hw = worker.hardware_specs or {}
        mode = hw.get("mode", "embedded")

        # Estimate phase durations
        phases = self._build_phase_estimates(task, worker, pattern, timeline)

        # Calculate total task resource requirements
        if pattern:
            peak_ram = pattern.avg_peak_ram_mb or 500
            peak_vram = _vram_demand_for_queue(task, pattern.avg_peak_vram_mb)
        else:
            # Conservative defaults
            peak_ram = 2000  # 2GB
            peak_vram = 0

        # Check if task can fit at all (even if we wait)
        if timeline.unified_memory:
            # MAC.1.s9a — one pool: the task's combined RAM+VRAM demand must
            # fit inside the shared ceiling. There is no separate VRAM device
            # to be smaller than, so the 85%-of-vram_gb fence does not apply
            # (a Mac's vram_gb is the same unified figure, not a card).
            if (peak_ram + peak_vram) > timeline.shared_ceiling_mb:
                return None
        else:
            if peak_ram > timeline.ram_ceiling_mb:
                return None  # Task requires more RAM than this worker has

            if peak_vram > 0 and peak_vram > timeline.vram_total_mb * 0.85:
                return None  # Task requires more VRAM than this worker has

        # Try to fit now
        total_phase_duration = sum(p["est_duration_s"] for p in phases)
        start_offset = 0.0

        if not timeline.can_fit_now(peak_ram, peak_vram):
            # Use predictive scheduling if we have a pattern
            if pattern and pattern.confidence >= 0.5:
                try:
                    from utils.predictive_scheduling import can_accept_with_prediction

                    required = {"ram_gb": peak_ram / 1024, "vram_gb": peak_vram / 1024}
                    if timeline.unified_memory:
                        # MAC.1.s9a — one pool: report the same shared figure
                        # on both axes so the predictor's combined demand is
                        # never charged against a second (nonexistent) device.
                        shared_gb = timeline.current_shared_available_mb / 1024
                        current_free = {
                            "ram_gb": shared_gb,
                            "vram_gb": shared_gb,
                            "cpu": timeline.current_cpu_available,
                        }
                    else:
                        current_free = {
                            "ram_gb": timeline.current_ram_available_mb / 1024,
                            "vram_gb": timeline.current_vram_available_mb / 1024,
                            "cpu": timeline.current_cpu_available,
                        }

                    decision = can_accept_with_prediction(
                        required_resources=required,
                        task_pattern=pattern.pattern_data,
                        current_free_resources=current_free,
                        running_tasks=[],
                        safety_margin=0.15,
                    )

                    if decision.get("can_accept"):
                        start_offset = decision.get("estimated_wait_seconds", 0)
                    else:
                        return None

                except ImportError:
                    return None
            else:
                return None

        # Build the phase plan
        start_after = now + timedelta(seconds=start_offset)
        phase_plan = {
            "phases": phases,
            "total_est_duration_s": total_phase_duration,
            "scheduling_confidence": pattern.confidence if pattern else 0.3,
            "scheduling_reasoning": f"Placed on {worker.worker_name} "
                                    f"({'immediate' if start_offset == 0 else f'start after {start_offset:.0f}s'})",
        }

        completion_estimate = start_after + timedelta(seconds=total_phase_duration)

        return Placement(
            worker_name=worker.worker_name,
            start_after=start_after,
            phase_plan=phase_plan,
            confidence=pattern.confidence if pattern else 0.3,
            reasoning=phase_plan["scheduling_reasoning"],
            completion_estimate=completion_estimate,
        )

    def _build_phase_estimates(self, task: ApiTaskQueue, worker: WorkerLicense,
                               pattern: Optional[ResourcePattern],
                               timeline: MultiResourceTimeline) -> List[dict]:
        """Build estimated phase plan for a task on a specific worker."""
        hw = worker.hardware_specs or {}
        cached_images = set(hw.get("cached_images", []))
        mode = hw.get("mode", "embedded")
        processing_image = (task.task_payload or {}).get("processing_image", "")

        # Peak resource estimates — mirror the logic in _try_fit_on_worker so
        # _pattern_aware_resources() below has values to work with. Without this,
        # the method hits a NameError on peak_ram/peak_vram and the caller logs
        # "Failed to schedule task N: name 'peak_ram' is not defined", which
        # leaves the task stuck in PENDING with no assigned_to_worker.
        if pattern:
            peak_ram = pattern.avg_peak_ram_mb or 500
            peak_vram = _vram_demand_for_queue(task, pattern.avg_peak_vram_mb)
        else:
            peak_ram = 2000  # Conservative 2GB default
            peak_vram = 0

        phases = []
        offset = 0.0

        # Phase 1: Image check
        image_check_duration = 2.0
        if mode == "remote":
            api_lat = hw.get("network", {}).get("api_latency_ms", 50)
            image_check_duration += api_lat / 1000.0
        phases.append({
            "name": "image_check",
            "start_offset_s": offset,
            "est_duration_s": image_check_duration,
            "resources": {"network_mbps": 1},
        })
        offset += image_check_duration

        # Phase 2: Image pull (skip if cached)
        if processing_image and processing_image not in cached_images:
            # Estimate pull time from NIC capacity and estimated image size
            est_image_size_mb = 2000  # Default 2GB image
            nic_throughput = effective_nic_throughput(
                timeline.nic_capacity_mbps,
                1 + int(timeline.current_nic_rx_mbps > 100)  # Account for existing transfers
            )
            pull_duration = max((est_image_size_mb * 8) / nic_throughput, 5.0)
            pull_duration += docker_operation_delay(timeline.current_docker_ops)

            phases.append({
                "name": "image_pull",
                "start_offset_s": offset,
                "est_duration_s": pull_duration,
                "resources": {
                    "network_mbps": nic_throughput,
                    "disk_iops": 200,
                    "docker_slots": 1,
                },
            })
            # Image pull and data download can overlap — don't advance offset fully
            pull_end = offset + pull_duration
        else:
            pull_end = offset
            # Even cached images need layer extraction
            mount_time = image_mount_seconds(2.0)  # Assume 2GB cached image
            phases.append({
                "name": "image_check",
                "start_offset_s": offset,
                "est_duration_s": mount_time,
                "resources": {"disk_iops": 100},
            })
            pull_end = offset + mount_time

        # Phase 3: Data download (can overlap with image pull)
        inputs = (task.task_payload or {}).get("inputs", [])
        total_input_bytes = sum(
            inp.get("file_size_bytes", 0) for inp in inputs if isinstance(inp, dict)
        )
        total_input_mb = total_input_bytes / (1024 * 1024)
        file_count = len(inputs)

        if total_input_mb > 0:
            # Location-aware transfer time
            if mode == "embedded":
                minio_throughput = 500.0  # Same Docker network
            elif mode == "datacenter":
                minio_throughput = hw.get("network", {}).get("minio_throughput_mbps", 200.0)
            else:
                minio_throughput = hw.get("network", {}).get("minio_throughput_mbps", 50.0)

            download_duration = (total_input_mb * 8) / max(minio_throughput, 1.0)
            download_duration += file_count * 0.05  # 50ms per-file S3 API overhead

            # Disk contention with image pull
            if pull_end > offset:
                download_duration *= (1.0 / disk_throughput_factor(1, 1))

            download_start = offset  # Overlap with pull
            phases.append({
                "name": "data_download",
                "start_offset_s": download_start,
                "est_duration_s": download_duration,
                "resources": {
                    "network_mbps": minio_throughput,
                    "disk_iops": 100,
                },
            })
            download_end = download_start + download_duration
        else:
            download_end = offset

        # Both pull and download must complete before container start
        ready_offset = max(pull_end, download_end)

        # Phase 4: Container start
        container_start_duration = 5.0
        container_start_duration += docker_operation_delay(timeline.current_docker_ops)
        container_start_duration += gc_pause_overhead_seconds(
            timeline.snapshot.active_containers if timeline.snapshot else 0
        )

        # GPU warmup if needed
        if (timeline.gpu_type != "none" and pattern
                and _vram_demand_for_queue(task, pattern.avg_peak_vram_mb) > 0):
            container_start_duration += gpu_warmup_seconds(timeline.current_gpu_power_state)

        phases.append({
            "name": "container_start",
            "start_offset_s": ready_offset,
            "est_duration_s": container_start_duration,
            "resources": {
                "ram_mb": 500,
                "cpu_cores": 2,
                "docker_slots": 1,
                "vram_mb": _vram_demand_for_queue(task, pattern.avg_peak_vram_mb) if pattern else 0,
            },
        })
        processing_start = ready_offset + container_start_duration

        # Phase 5: Processing (main resource curve)
        if pattern and pattern.avg_duration_seconds:
            processing_duration = pattern.avg_duration_seconds

            # #90: Input-size-aware scaling — scale duration based on actual input
            # size relative to the average input size from historical runs.
            if pattern.avg_input_size_mb and pattern.avg_input_size_mb > 0 and total_input_mb > 0:
                size_ratio = total_input_mb / pattern.avg_input_size_mb
                # Sub-linear scaling: doubling input doesn't double duration
                # (most processing has fixed overhead + linear component)
                scaling_factor = 0.3 + 0.7 * size_ratio  # 30% fixed + 70% scales
                processing_duration *= max(scaling_factor, 0.5)  # Floor at 50% of average
                logger.debug("Input-size scaling: %.1fMB vs avg %.1fMB → factor %.2f → %.1fs",
                             total_input_mb, pattern.avg_input_size_mb, scaling_factor, processing_duration)
        else:
            # Cold start (no pattern or <3 samples). Bucket H-1 will plumb the
            # ONNX predictor in here; for now hold the conservative 120s
            # default. The previous TimesFM cold-start path was removed
            # (broken sys.path import; we standardize on the ONNX scheduler
            # model trained from real production samples instead of the
            # zero-shot TimesFM forecaster, which never had an actual training
            # signal feeding it).
            processing_duration = 120.0

        # Thermal throttle adjustment
        if timeline.thermal_throttled:
            processing_duration *= (1.0 / thermal_cpu_factor(timeline.current_cpu_temp_c, True))

        # #94: Pattern-type-aware resource reservation.
        # When we know the resource curve shape from pattern_recognition.py,
        # use it to set smarter resource reservations instead of reserving
        # peak for the entire duration.
        processing_resources = self._pattern_aware_resources(pattern, peak_ram, peak_vram)

        phases.append({
            "name": "processing",
            "start_offset_s": processing_start,
            "est_duration_s": processing_duration,
            "resources": processing_resources,
        })
        upload_start = processing_start + processing_duration

        # Phase 6: Upload
        if pattern and pattern.avg_output_size_mb and pattern.avg_output_size_mb > 0:
            output_mb = pattern.avg_output_size_mb
        else:
            output_mb = total_input_mb * 1.5  # Assume output ~1.5x input

        if output_mb > 0:
            if mode == "embedded":
                upload_throughput = 500.0
            else:
                upload_throughput = hw.get("network", {}).get("minio_throughput_mbps", 50.0)

            upload_duration = (output_mb * 8) / max(upload_throughput, 1.0)
            phases.append({
                "name": "upload",
                "start_offset_s": upload_start,
                "est_duration_s": upload_duration,
                "resources": {"network_mbps": upload_throughput, "disk_iops": 50},
            })
            cleanup_start = upload_start + upload_duration
        else:
            cleanup_start = upload_start

        # Phase 7: Cleanup
        phases.append({
            "name": "cleanup",
            "start_offset_s": cleanup_start,
            "est_duration_s": 2.0,
            "resources": {"docker_slots": 1},
        })

        # Add remote worker overhead
        if mode == "remote":
            api_lat = hw.get("network", {}).get("api_latency_ms", 100)
            overhead = remote_worker_overhead_seconds(api_lat)
            # Distribute overhead across phases
            for p in phases:
                p["est_duration_s"] += overhead / len(phases)

        return phases

    def _pattern_aware_resources(self, pattern: Optional[ResourcePattern],
                                 peak_ram: float, peak_vram: float) -> dict:
        """
        #94: Build resource reservation dict using pattern_recognition curve type.

        Instead of always reserving peak resources for the full processing
        phase, use the classified pattern type to describe how resources are
        actually consumed over time.  The contention model can then use this
        to allow more aggressive overlap.

        Pattern behaviours:
        - plateau / step_function: resources jump to peak early and hold.
            Reserve at the full peak (safe, no benefit from over-committing).
        - linear_ramp: starts low, ramps to peak over the full duration.
            Reserve the midpoint on average; expose ramp metadata.
        - sawtooth: periodic spikes then drops.
            Average usage is roughly half the peak.
        - declining: starts at peak and decreases.
            Front-load the reservation, tail can overlap.
        - unknown / None: fall back to peak or conservative default.
        """
        if not pattern:
            return {"ram_mb": peak_ram, "cpu_cores": 2}

        pt = (pattern.pattern_type or "unknown").lower()
        pd = pattern.pattern_data or {}
        metrics = pd.get("metrics", {})

        if pt == "plateau" or pt == "step_function":
            # Consistent high usage — reserve the full peak.
            plateau_val = metrics.get("plateau_value")
            ram_reservation = plateau_val if plateau_val and plateau_val > 0 else peak_ram
            return {
                "ram_mb": ram_reservation,
                "vram_mb": peak_vram,
                "cpu_cores": 2,
                "pattern_type": pt,
                "reservation_strategy": "full_peak",
            }

        if pt == "linear_ramp":
            # Resources increase linearly.  On average usage is ~midpoint.
            start_val = metrics.get("start_value", peak_ram * 0.25)
            end_val = metrics.get("end_value", peak_ram)
            avg_reservation = (start_val + end_val) / 2.0
            return {
                "ram_mb": peak_ram,           # Hard cap stays at peak
                "ram_mb_avg": avg_reservation, # Average for overlap math
                "vram_mb": peak_vram,
                "cpu_cores": 2,
                "pattern_type": pt,
                "reservation_strategy": "ramping",
                "ramp_rate_per_second": metrics.get("ramp_rate_per_second", 0),
            }

        if pt == "sawtooth":
            # Periodic spikes — average usage is roughly half the peak.
            peak_val = metrics.get("peak_value", peak_ram)
            valley_val = metrics.get("valley_value", peak_ram * 0.3)
            avg_reservation = (peak_val + valley_val) / 2.0
            return {
                "ram_mb": peak_ram,            # Hard cap at peak for spike moments
                "ram_mb_avg": avg_reservation,  # Average for overlap math
                "vram_mb": peak_vram,
                "cpu_cores": 2,
                "pattern_type": pt,
                "reservation_strategy": "periodic_spikes",
                "cycle_count": metrics.get("cycle_count", 0),
                "amplitude": metrics.get("amplitude", 0),
            }

        if pt == "declining":
            # Starts high, decreases over time.  Tail can overlap safely.
            start_val = metrics.get("start_value", peak_ram)
            end_val = metrics.get("end_value", peak_ram * 0.25)
            avg_reservation = (start_val + end_val) / 2.0
            return {
                "ram_mb": peak_ram,            # Hard cap (peak at start)
                "ram_mb_avg": avg_reservation,  # Average for overlap math
                "vram_mb": peak_vram,
                "cpu_cores": 2,
                "pattern_type": pt,
                "reservation_strategy": "front_loaded",
                "decline_rate_per_second": metrics.get("decline_rate_per_second", 0),
            }

        # Unknown or unrecognised — reserve at peak (conservative).
        return {
            "ram_mb": peak_ram,
            "vram_mb": peak_vram,
            "cpu_cores": 2,
            "pattern_type": pt,
            "reservation_strategy": "conservative",
        }

    def _select_best(self, task: ApiTaskQueue, placements: List[Placement],
                     db: Session) -> Placement:
        """Rank placements and return the best one."""

        def score(p: Placement) -> tuple:
            # Sort key: (earliest completion, highest confidence)
            # Lower completion time is better, higher confidence is better
            return (p.completion_estimate, -p.confidence)

        placements.sort(key=score)
        return placements[0]

    def _get_eligible_workers(self, task: ApiTaskQueue, db: Session) -> List[WorkerLicense]:
        """Get workers that are licensed, active, and authorized for the task."""
        workers = db.query(WorkerLicense).filter(
            WorkerLicense.status == WorkerStatus.LICENSED,
            WorkerLicense.paused == False,           # noqa: E712
            WorkerLicense.update_requested == False,  # noqa: E712
            WorkerLicense.deleted_at.is_(None),
        ).all()

        from utils.paid_import import optional_paid
        _noah = optional_paid("services.noah")
        if _noah is None:
            _is_noah_worker = lambda _db, _w: False
        else:
            _is_noah_worker = _noah.is_noah_worker
        eligible = []
        for w in workers:
            # NOAH: opportunistic workers are pull-only. Never pre-place
            # (ASSIGN) work to them — a pinned task on a machine that goes
            # unavailable strands until released. They remain fully eligible to
            # PULL unpinned pool work via the normal claim path. This is a no-op
            # when no worker is NOAH-enabled.
            if _is_noah_worker(db, w):
                continue

            # Check queue support
            if w.queues and task.queue_name not in w.queues:
                continue

            # Check tenant authorization (handles wildcard for SaaS pools)
            from routers.worker_tasks import _get_authorized_tenants, _is_tenant_authorized
            authorized = _get_authorized_tenants(w)
            if not _is_tenant_authorized(authorized, task.tenant_id):
                continue

            # Check profiling gate — workers still benchmarking can only
            # run system tasks (workflow_run_id IS NULL).
            #
            # Exception: the task we are trying to place IS itself the
            # benchmark for this worker's profile. Without this exemption,
            # if the first dispatch attempt fails (image pull 404, worker
            # restart, etc.) and the task reverts to PENDING, the scheduler
            # can never re-place it on its own target worker, because the
            # worker is flagged as "benchmarking." Profiling runs carry
            # `parameters.is_profiling = True`; let those through.
            if task.workflow_run_id is not None:
                from models import HardwareProfile, WorkflowRun
                is_profiling_task = False
                run = db.query(WorkflowRun).filter(
                    WorkflowRun.id == task.workflow_run_id
                ).first()
                if run and (run.parameters or {}).get("is_profiling"):
                    is_profiling_task = True
                if not is_profiling_task and w.hardware_profile_id:
                    hp = db.query(HardwareProfile).filter(
                        HardwareProfile.id == w.hardware_profile_id
                    ).first()
                    if hp and hp.benchmark_status in ("pending", "running"):
                        continue

            eligible.append(w)

        return eligible

    def _get_snapshot(self, worker_name: str, db: Session) -> Optional[WorkerResourceTimeline]:
        """Get the latest resource snapshot for a worker (cache first, then DB)."""
        with self._cache_lock:
            cached = self._snapshot_cache.get(worker_name)
            if cached:
                return cached

        return db.query(WorkerResourceTimeline).filter(
            WorkerResourceTimeline.worker_name == worker_name
        ).first()

    def _extract_node_kind(self, task: ApiTaskQueue) -> Optional[str]:
        if not task.task_payload:
            return None
        # Support both nested and flat payload formats
        node_config = task.task_payload.get("node_config", {})
        return (task.task_payload.get("node_kind")
                or node_config.get("node_kind")
                or node_config.get("process_dag_id"))

    def _get_pattern(self, node_kind: Optional[str], queue_name: str,
                     db: Session) -> Optional[ResourcePattern]:
        """
        #90/#91/#92: Get resource pattern with GPU-type awareness and similar-node inference.

        Lookup chain:
        1. Exact match: node_kind + queue_name + gpu_model
        2. GPU-agnostic: node_kind + queue_name (any GPU)
        3. Similar node inference (#91): same queue + similar category
        """
        if not node_kind:
            return None

        # 1. Exact match (includes GPU-specific patterns from #92)
        pattern = db.query(ResourcePattern).filter(
            ResourcePattern.node_kind == node_kind,
            ResourcePattern.queue_name == queue_name,
        ).order_by(ResourcePattern.confidence.desc()).first()

        if pattern and pattern.confidence > 0.3:
            return pattern

        # 2. Similar-node inference (#91) — find patterns from nodes with the same
        # category suffix (e.g., if "custom_albedo" has no history, try "albedo")
        base_type = node_kind.rsplit("_", 1)[-1] if "_" in node_kind else node_kind
        similar = db.query(ResourcePattern).filter(
            ResourcePattern.node_kind.like(f"%{base_type}"),
            ResourcePattern.queue_name == queue_name,
            ResourcePattern.confidence > 0.3,
        ).order_by(ResourcePattern.confidence.desc()).first()

        if similar:
            logger.info("Using similar-node pattern %s for %s (confidence=%.2f)",
                        similar.node_kind, node_kind, similar.confidence)
            return similar

        # 3. Fallback: any pattern for this queue with decent confidence.
        #
        # This is a prior on "what a node on this queue tends to cost", and it
        # is only meaningful once the corpus actually spans a variety of nodes.
        # Below that floor it is not a prior, it is one node's profile wearing
        # every other node's name: measured here with a corpus of 3 patterns,
        # it would have handed image_converter's 23s CPU profile to all 131
        # node kinds, including hour-long renders. Mispredicting a 3600s task
        # as 23s packs it onto a worker that then blocks everything queued
        # behind it — worse than not predicting at all, because no prediction
        # falls back to ordinary queueing while a wrong one actively misplaces.
        #
        # Returning None makes schedule_task decline the placement, which is
        # the safe degradation. The floor is what lets PREDICTIVE_SCHEDULING
        # be switched on before calibration has covered the whole node set.
        min_kinds = int(os.getenv("SCHEDULER_QUEUE_FALLBACK_MIN_KINDS", "10"))
        corpus = db.query(ResourcePattern.node_kind).filter(
            ResourcePattern.queue_name == queue_name,
            ResourcePattern.confidence > 0.5,
        ).distinct().count()

        if corpus < min_kinds:
            logger.debug(
                "No queue fallback for %s on %s: corpus has %d distinct node "
                "kinds, need %d before a queue-wide prior means anything",
                node_kind, queue_name, corpus, min_kinds,
            )
            return None

        fallback = db.query(ResourcePattern).filter(
            ResourcePattern.queue_name == queue_name,
            ResourcePattern.confidence > 0.5,
        ).order_by(ResourcePattern.sample_count.desc()).first()

        if fallback:
            logger.debug("Using queue fallback pattern %s for %s", fallback.node_kind, node_kind)

        return fallback

    # =========================================================================
    # Background Scheduling Loop
    # =========================================================================

    def start_background_loop(self):
        """Start the background scheduling thread."""
        if not PREDICTIVE_SCHEDULING_ENABLED:
            logger.info("Predictive scheduling disabled, background loop not started")
            return
        if not _advanced_scheduling_allowed():
            logger.info(
                "Cap.ADVANCED_SCHEDULING not granted by this deployment's "
                "entitlements — overlap scheduler background loop not "
                "started (FIFO only)"
            )
            return

        self._thread = threading.Thread(target=self._loop, daemon=True, name="overlap-scheduler")
        self._thread.start()
        logger.info("Overlap scheduler background loop started (interval=%.1fs)", SCHEDULING_LOOP_INTERVAL)

    def stop(self):
        """Stop the background loop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self):
        """Background loop: process PENDING tasks and handle stale assignments."""
        while not self._stop_event.is_set():
            try:
                db = self.db_factory()
                try:
                    self._process_pending_tasks(db)
                    self._expire_stale_assignments(db)
                    self._handle_offline_workers(db)
                    self._expire_stuck_profiling_runs(db)
                finally:
                    db.close()
            except Exception as e:
                logger.error("Scheduler loop error: %s", e, exc_info=True)

            self._stop_event.wait(timeout=SCHEDULING_LOOP_INTERVAL)

    def _process_pending_tasks(self, db: Session):
        """Try to place all PENDING tasks.

        With BATCH_PACKING_ENABLED, runs the QueuePacker first to
        batch-optimise placements (2-opt makespan reduction across all
        pending tasks simultaneously), then falls back to the per-task
        greedy path for anything the packer couldn't place or if the
        packer errors out.

        Re-checks Cap.ADVANCED_SCHEDULING on every tick (not just at
        start_background_loop) because the batch packer below writes
        placements directly and does not route through schedule_task's own
        gate — without this, a license that lapses after the loop is
        already running would keep batch-packing on an ungated deployment
        until the next process restart.
        """
        if not _advanced_scheduling_allowed(db):
            return
        pending = db.query(ApiTaskQueue).filter(
            ApiTaskQueue.status == ApiTaskStatus.PENDING,
        ).order_by(
            ApiTaskQueue.priority.desc(),
            ApiTaskQueue.created_at.asc(),
        ).limit(50).all()

        # GX.D1 — placement-pinned tasks are NOT pre-placed. The packer and
        # the greedy path both assign by score, with no notion of a pinned
        # worker/tag; handing one of these to a non-matching worker would
        # strand it (a pinned task is claimable only by the matching worker,
        # per the claim SQL). They stay PENDING and claim themselves via the
        # worker's fallback claim path, which enforces the pin.
        from services.workflow_placement import _is_placement_pinned
        pending = [t for t in pending
                   if not _is_placement_pinned(t.task_payload)]

        # Durable pins (benchmark / host-pinned stack tasks) may only ever be
        # placed on their pinned worker — never scored into the batch packer
        # or the greedy path, both of which would hand them to another
        # machine. Assign them straight to the pin (the dispatch path usually
        # creates them ASSIGNED already; this covers rows a sweep re-pooled
        # back to PENDING). When the pinned worker row is gone we hold
        # PENDING — the pin never moves to a different host.
        from utils.task_pinning import durable_target_worker
        pinned_pending = [t for t in pending if durable_target_worker(t)]
        if pinned_pending:
            pending = [t for t in pending if not durable_target_worker(t)]
            from sqlalchemy import text as _pin_sql
            for task in pinned_pending:
                pin = durable_target_worker(task)
                pinned_w = (db.query(WorkerLicense)
                            .filter(WorkerLicense.worker_name == pin).first())
                if pinned_w is None:
                    logger.warning(
                        "Task %d is pinned to %s, which is not a known worker — "
                        "holding PENDING (the pin never moves to another host)",
                        task.id, pin,
                    )
                    continue
                row = db.execute(
                    _pin_sql("""
                        UPDATE api_task_queue
                        SET status = :assigned,
                            assigned_to_worker = :worker
                        WHERE id = :task_id
                          AND status = :pending
                        RETURNING id
                    """),
                    {
                        "assigned": ApiTaskStatus.ASSIGNED.name,
                        "pending": ApiTaskStatus.PENDING.name,
                        "worker": pin,
                        "task_id": task.id,
                    },
                ).fetchone()
                if row is not None:
                    db.commit()
                    logger.info(
                        "Task %d (durable pin) placed on its pinned worker %s",
                        task.id, pin,
                    )

        placed_ids: set = set()
        if BATCH_PACKING_ENABLED and len(pending) > 1:
            try:
                placed_ids = self._batch_pack_pending(pending, db)
            except Exception as e:
                logger.warning("Batch packer failed, falling back to per-task greedy: %s", e)

        for task in pending:
            if task.id in placed_ids:
                continue
            # Layer-4 staleness gate: if the node's scan is stale, enqueue
            # a rescan and defer this task — don't dispatch workloads
            # against security state the CVE DB has moved past. Noops on
            # on-prem (no scanner dispatch there anyway).
            try:
                from utils.freshness import pre_dispatch_stale_check
                if pre_dispatch_stale_check(db, task):
                    continue
            except Exception as e:  # never block scheduling on freshness errors
                logger.warning("freshness check failed for task %d: %s", task.id, e)

            try:
                self.schedule_task(task, db)
            except Exception as e:
                logger.warning("Failed to schedule task %d: %s", task.id, e)

    def _batch_pack_pending(self, pending: list, db: Session) -> set:
        """Batch-optimise placement for a bundle of PENDING tasks.

        Returns the set of task IDs that were placed. Anything not in
        the returned set falls back to the per-task greedy loop.

        Implementation notes:
          - Predictions come from ResourcePatterns (existing pattern
            store — same source the per-task path uses). No ONNX model
            required; if a task's dag has no pattern we synthesise a
            defaults-based one so the packer can still score it.
          - TimelineSimulators are seeded from WorkerLicense resource
            columns + current in-flight tasks' projected remaining
            work. Workers in red memory zone or thermal throttling are
            dropped before packing.
        """
        from scheduler.queue_packer import QueuePacker
        from scheduler.timeline_simulator import TimelineSimulator, PlannedTask
        from scheduler.onnx_predictor import PredictionResult as PRes

        # 1. Eligible workers — use OverlapScheduler's existing
        #    per-task filter against the first task (conservative:
        #    anything that can host task[0] is in the pool). Tasks with
        #    different kind/gpu needs get handled in the fallback loop.
        anchor = pending[0]
        workers = self._get_eligible_workers(anchor, db)
        if not workers:
            return set()

        # 2. Build timelines + discard bad-state workers
        worker_timelines: dict = {}
        for w in workers:
            try:
                snapshot = self._get_snapshot(w.worker_name, db)
                # WorkerResourceTimeline columns are ram_total_mb /
                # vram_total_mb (not total_ram_mb / total_vram_mb), and
                # there is no cpu_cores column on the timeline at all —
                # cpu_cores lives on WorkerLicense.hardware_specs (JSONB).
                # The mismatched attribute lookup made every batch_pack
                # tick raise AttributeError and silently fall back to
                # the per-task greedy path, never exercising 2-opt or
                # the adaptive budget.
                hw = w.hardware_specs or {}
                _tl_ram_mb = float(
                    getattr(snapshot, "ram_total_mb", None)
                    or (hw.get("ram_gb", 0) * 1024)
                    or 0
                )
                _tl_vram_mb = float(
                    getattr(snapshot, "vram_total_mb", None)
                    or (hw.get("vram_gb", 0) * 1024)
                    or 0
                )
                # MAC.1.s9b — hand the Metal lane its ONE pool (max, never
                # the sum) so the batch packer can't double-book RAM ∪ VRAM
                # on an Apple box. Detection is reused from this module's
                # helper; discrete workers get None and the old two-device
                # maths is untouched.
                _tl_shared_mb = (
                    max(_tl_ram_mb, _tl_vram_mb)
                    if _is_unified_memory_worker(w) else None
                )
                tl = TimelineSimulator(
                    total_ram_mb=_tl_ram_mb,
                    total_vram_mb=_tl_vram_mb,
                    cpu_cores=int(hw.get("cpu_cores") or 1),
                    nic_capacity_mbps=float(getattr(snapshot, "nic_capacity_mbps", 1000) or 1000),
                    shared_pool_mb=_tl_shared_mb,
                )
                worker_timelines[w.worker_name] = tl
            except Exception as e:
                logger.debug("Skipping worker %s in batch pack: %s", w.worker_name, e)

        if not worker_timelines:
            return set()

        # Deferred-5b — DAG-aware lookahead. Before greedy placement of the
        # currently-runnable PENDING set, walk active WorkflowRuns and emit
        # reservations for blocked NodeRuns (those whose parent NodeRuns
        # haven't completed yet). Each reservation is applied to the
        # lowest-makespan eligible worker as a no-op timeline placeholder
        # so the round's placement doesn't pack tightly into windows that
        # blocked tasks will need. Reservations are ephemeral — they live
        # in the in-memory worker_timelines for this round only and never
        # write to ApiTaskQueue / NodeRun. Disabled via
        # PACKER_DAG_LOOKAHEAD_ENABLED=false for ops who want greedy-only.
        try:
            if os.getenv("PACKER_DAG_LOOKAHEAD_ENABLED", "true").lower() != "false":
                from scheduler.dag_lookahead import (
                    collect_lookahead_reservations,
                    apply_reservations_to_timelines,
                )
                reservations = collect_lookahead_reservations(db)
                if reservations:
                    applied = apply_reservations_to_timelines(reservations, worker_timelines)
                    logger.info(
                        "DAG lookahead: applied %d/%d reservations to worker_timelines",
                        applied, len(reservations),
                    )
        except Exception as e:
            logger.debug("DAG lookahead failed (non-fatal): %s", e)

        # 3. Build predictions from existing ResourcePatterns (no ONNX).
        #    Missing patterns → conservative synthetic defaults so the
        #    packer still considers the task (same numbers the per-task
        #    fallback uses — see _placement_resources line ~490).
        # Bucket G — wrap pattern lookup in the Qdrant prediction cache
        # so identical placements (same dag, same hw_profile_id, same
        # contention summary) reuse prior predictions instead of recomputing.
        # Cache misses fall through to the existing pattern_to_prediction
        # path; failures (Qdrant down, cache module unavailable) fall through
        # silently and never block placement.
        from scheduler import prediction_cache as _pcache
        predictions: dict = {}
        # Build a coarse contention summary per worker — H-1 pass uses
        # current-load + concurrent-task-count from heartbeat snapshots.
        # H-2 will switch this to time-windowed phase-resolved curves.
        workers_by_name = {w.worker_name: w for w in workers}
        worker_contention: dict = {}
        for worker_name, sim in worker_timelines.items():
            worker_contention[worker_name] = {
                "concurrent_task_count": float(getattr(sim, "_active_count", 0) or 0),
                "current_load_pct": float(getattr(sim, "_current_load_pct", 0.0) or 0.0),
            }

        for task in pending:
            node_kind = self._extract_node_kind(task) or ""
            pattern = self._get_pattern(node_kind, task.queue_name, db)
            for worker_name in worker_timelines:
                worker = workers_by_name.get(worker_name)
                hw_profile_id = (worker.hardware_profile_id if worker else 0) or 0

                cached = None
                try:
                    cached = _pcache.get(node_kind, hw_profile_id, worker_contention.get(worker_name, {}))
                except Exception:
                    cached = None

                if cached is not None:
                    from scheduler.onnx_predictor import PredictionResult as PRes
                    try:
                        pred = PRes(
                            processing_duration_s=float(cached.get("processing_duration_s", 120.0)),
                            upload_duration_s=float(cached.get("upload_duration_s", 5.0)),
                            download_duration_s=float(cached.get("download_duration_s", 5.0)),
                            pattern_type=str(cached.get("pattern_type", "unknown")),
                            pattern_confidence=float(cached.get("pattern_confidence") or cached.get("confidence") or 0.5),
                            peak_ram_mb=float(cached.get("peak_ram_mb", 1024.0)),
                            peak_vram_mb=float(cached.get("peak_vram_mb", 0.0)),
                            peak_cpu_pct=float(cached.get("peak_cpu_pct", 50.0)),
                            peak_net_mbps=float(cached.get("peak_net_mbps", 50.0)),
                            model_version=int(cached.get("model_version", 0)),
                            inference_mode=str(cached.get("inference_mode", "cache")),
                        )
                        predictions[(task.id, worker_name)] = pred
                        continue
                    except Exception:
                        pass

                pred = self._pattern_to_prediction(pattern, task)
                predictions[(task.id, worker_name)] = pred
                # Stash for next round.
                try:
                    _pcache.put(node_kind, hw_profile_id, worker_contention.get(worker_name, {}), pred)
                except Exception:
                    pass

        # 4. Build task-info dicts the packer expects.
        # H-2 — surface attempt_count + the workers from prior failed attempts
        # (parsed out of scheduling_metadata.prior_workers if the orchestrator
        # populated it; otherwise empty) so the packer's retry-aware path
        # can exclude already-broken workers from the candidate set.
        task_infos = []
        for t in pending:
            sched_meta = t.scheduling_metadata or {}
            prior_workers = sched_meta.get("prior_workers") or []
            # If we don't have explicit prior_workers but the task has
            # claimed_by_worker on a non-first attempt, the most recent
            # worker counts.
            if (t.attempt_count or 0) > 0 and t.claimed_by_worker and t.claimed_by_worker not in prior_workers:
                prior_workers = list(prior_workers) + [t.claimed_by_worker]
            task_infos.append({
                "task_id": t.id,
                "node_kind": self._extract_node_kind(t),
                "priority": t.priority or 0,
                "priority_class": getattr(t, "priority_class", "normal") or "normal",
                "attempt_count": int(t.attempt_count or 0),
                "prior_workers": prior_workers,
            })

        # 5. Run the packer — pass the loaded ONNX predictor (may be None if
        # MinIO didn't have a model at startup; the packer tolerates None and
        # falls back to the precomputed `predictions` dict).
        try:
            from main import get_onnx_predictor
            onnx_pred = get_onnx_predictor()
        except Exception:
            onnx_pred = None
        packer = QueuePacker(onnx_predictor=onnx_pred)
        result = packer.pack(task_infos, worker_timelines, predictions, db=db)

        if not result.assignments:
            return set()

        # 5b. Log every placement decision to PredictionLog so we have a
        # complete prediction-vs-actual trail for the auto-retrain trigger
        # and accuracy monitoring. Failures here must not break placement —
        # logging is observational, not load-bearing.
        try:
            self._log_placement_predictions(db, result.assignments, predictions, onnx_pred)
        except Exception as e:
            logger.debug("PredictionLog write failed (non-fatal): %s", e)

        # 6. Apply assignments — same PENDING-only race guard as
        # schedule_task() (single-task path). The batch packer can run
        # for tens of milliseconds; if the embedded worker drained any
        # of the input PENDING tasks during that window via the
        # fallback claim path, we must NOT clobber CLAIMED/RUNNING
        # back to ASSIGNED — that's the double-execution bug observed
        # on run 7 task 497 (push-intel) where two skopeo containers
        # raced to push the same image.
        from models import ApiTaskStatus
        from sqlalchemy import text as _sql_text
        placed = set()
        skipped_claimed = 0
        for task_id, assignment in result.assignments.items():
            start_after = datetime.now(timezone.utc) + timedelta(
                seconds=max(0.0, float(assignment.start_after_s))
            )
            sched_meta = {
                "packer": "QueuePacker",
                "makespan_contrib_s": float(assignment.planned_task.processing_duration_s),
                "confidence": float(assignment.confidence),
                "complementary_score": float(assignment.complementary_score),
                "placed_at": datetime.now(timezone.utc).isoformat(),
                "used_2opt": result.used_2opt,
            }
            row = db.execute(
                _sql_text("""
                    UPDATE api_task_queue
                    SET status = :assigned,
                        assigned_to_worker = :worker,
                        start_after = :start_after,
                        scheduling_metadata = :scheduling_metadata
                    WHERE id = :task_id
                      AND status = :pending
                    RETURNING id
                """),
                {
                    "assigned": ApiTaskStatus.ASSIGNED.name,
                    "pending": ApiTaskStatus.PENDING.name,
                    "worker": assignment.worker_name,
                    "start_after": start_after,
                    "scheduling_metadata": json.dumps(sched_meta),
                    "task_id": task_id,
                },
            ).fetchone()
            if row is None:
                skipped_claimed += 1
                continue
            placed.add(task_id)

        if placed or skipped_claimed:
            db.commit()
            logger.info(
                "Batch-packed %d/%d tasks across %d workers (makespan=%.1fs, "
                "2opt=%s, %.0fms, skipped_claimed=%d)",
                len(placed), len(pending), len(worker_timelines),
                result.total_makespan_s, result.used_2opt,
                result.optimization_time_ms, skipped_claimed,
            )
        return placed

    def _pattern_to_prediction(self, pattern, task):
        """Adapt a ResourcePattern into the shape QueuePacker expects.

        Falls back to conservative defaults when a pattern is missing.
        Defaults chosen to match the per-task greedy fallback at
        _find_placement so batch + greedy paths don't diverge
        catastrophically on unseen DAGs."""
        from scheduler.onnx_predictor import PredictionResult as PRes
        if pattern is not None:
            return PRes(
                processing_duration_s=float(pattern.avg_duration_s or 120.0),
                upload_duration_s=float(getattr(pattern, "upload_duration_s", 5.0) or 5.0),
                download_duration_s=float(getattr(pattern, "download_duration_s", 5.0) or 5.0),
                pattern_type=str(getattr(pattern, "pattern_type", "unknown") or "unknown"),
                pattern_confidence=float(getattr(pattern, "confidence", 0.5) or 0.5),
                peak_ram_mb=float(getattr(pattern, "peak_ram_mb", 1024) or 1024),
                peak_vram_mb=float(getattr(pattern, "peak_vram_mb", 0) or 0),
                peak_cpu_pct=float(getattr(pattern, "peak_cpu_pct", 50) or 50),
                peak_net_mbps=float(getattr(pattern, "peak_net_mbps", 50) or 50),
                model_version=0,
                inference_mode="pattern-db",
            )
        return PRes(
            processing_duration_s=120.0,
            upload_duration_s=5.0,
            download_duration_s=5.0,
            pattern_type="unknown",
            pattern_confidence=0.1,
            peak_ram_mb=1024.0,
            peak_vram_mb=0.0,
            peak_cpu_pct=50.0,
            peak_net_mbps=50.0,
            model_version=0,
            inference_mode="synthetic-default",
        )

    def _log_placement_predictions(self, db: Session, assignments, predictions, onnx_pred):
        """Insert one PredictionLog row per placement decision.

        Captures predicted_duration_s + peaks + confidence + inference_mode
        and the model_version (when ONNX is loaded; 0 for pattern-db /
        synthetic-default fallbacks). actual_duration_s + actual_pattern +
        prediction_error_pct are backfilled later by pattern_updater on
        task completion — that closes the feedback loop the auto-retrain
        trigger reads.
        """
        from models import PredictionLog
        version = getattr(onnx_pred, "version", 0) if onnx_pred is not None else 0
        rows = []
        for task_id, assignment in assignments.items():
            pred = predictions.get((task_id, assignment.worker_name)) if predictions else None
            if pred is None:
                continue
            rows.append(PredictionLog(
                task_id=task_id,
                model_version=version,
                inference_mode=getattr(pred, "inference_mode", "unknown"),
                predicted_duration_s=float(pred.processing_duration_s or 0.0),
                predicted_pattern=str(getattr(pred, "pattern_type", "unknown")),
                predicted_peak_ram_mb=float(getattr(pred, "peak_ram_mb", 0) or 0),
                predicted_peak_vram_mb=float(getattr(pred, "peak_vram_mb", 0) or 0),
                confidence=float(getattr(pred, "pattern_confidence", 0) or 0),
            ))
        if rows:
            db.add_all(rows)
            db.commit()

    def _expire_stale_assignments(self, db: Session):
        """Revert ASSIGNED tasks that haven't been claimed within the expiry window."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=ASSIGNMENT_EXPIRY_MINUTES)

        stale = db.query(ApiTaskQueue).filter(
            ApiTaskQueue.status == ApiTaskStatus.ASSIGNED,
            ApiTaskQueue.updated_at < cutoff,
        ).all()

        for task in stale:
            # Durable-pin exception: a task pinned to a specific worker must
            # NOT be reverted to PENDING here — an un-pinned row would be
            # claimed by ANY worker.
            #   benchmark_target_worker — a hardware benchmark measures ONE
            #   machine; re-pooling it would let another worker's scores land
            #   on the pinned worker's profile (observed live: the arm64
            #   worker's profile got the AlphaBox's CPU scores). Its lifecycle is
            #   owned by _expire_stuck_profiling_runs, which force-fails the
            #   run with the real reason (never-claimed / arch mismatch) after
            #   PROFILING_MAX_RUNTIME_MINUTES.
            #   pinned_worker — an operator's host pin on a persistent stack.
            #   It must hold for the whole life of the stack's tasks, so the
            #   task stays ASSIGNED on its host until the pinned worker claims
            #   it (2026-10-03: stack 39's deploy was reverted + re-assigned to
            #   other hosts by this sweep).
            from utils.task_pinning import benchmark_target_worker, durable_target_worker
            pin = durable_target_worker(task)
            if pin:
                if benchmark_target_worker(task):
                    logger.info(
                        "Task %d assignment past %dm but benchmark-pinned to %s — "
                        "left ASSIGNED (profiling-deadline expirer owns its lifecycle)",
                        task.id, ASSIGNMENT_EXPIRY_MINUTES, task.assigned_to_worker,
                    )
                else:
                    _payload = task.task_payload or {}
                    _stack_id = (
                        _payload.get("stack_id") if isinstance(_payload, dict) else None
                    )
                    # Hold + make it visible, once per sweep: the task stays
                    # ASSIGNED on the pinned worker and the wait is reported on
                    # the stack itself (status_reason is the operator-facing
                    # reason; cleared on a successful transition).
                    logger.info(
                        "Task %d assignment past %dm but host-pinned to %s "
                        "(stack %s) — left ASSIGNED for the pinned worker to claim",
                        task.id, ASSIGNMENT_EXPIRY_MINUTES, pin, _stack_id,
                    )
                    if _stack_id is not None:
                        try:
                            from models import PersistentStack
                            _stack = (db.query(PersistentStack)
                                      .filter(PersistentStack.id == _stack_id).first())
                            if _stack is not None:
                                _stack.status_reason = (
                                    f"waiting for pinned worker {pin} to claim")
                        except Exception:  # noqa: BLE001 — the hold must never break the sweep
                            logger.debug(
                                "host-pin status_reason update failed for task %d",
                                task.id, exc_info=True)
                continue
            logger.warning("Task %d assignment expired (assigned to %s), reverting to PENDING",
                           task.id, task.assigned_to_worker)
            task.status = ApiTaskStatus.PENDING
            task.assigned_to_worker = None
            task.scheduling_version = (task.scheduling_version or 0) + 1

        if stale:
            db.commit()

    def _profiling_task_failure_reason(self, task: "ApiTaskQueue") -> str:
        """A human-readable cause for a profiling task that never finished.

        The old flat message ("Check Harbor credentials and worker connectivity")
        misled on the most common real cause: a benchmark image with no manifest
        for the worker's CPU architecture. The worker's claim guard treats an
        unconfirmed image as amd64-only and simply never claims the task, so
        `claimed_at` is NULL even though the worker is healthy and online. Say
        that (naming the image) instead of blaming Harbor/creds.
        """
        try:
            payload = task.task_payload or {}
        except Exception:  # noqa: BLE001
            payload = {}
        node_data = payload.get("node_data") if isinstance(payload, dict) else None
        if not isinstance(node_data, dict):
            node_data = payload if isinstance(payload, dict) else {}
        image = node_data.get("processing_image") or (
            payload.get("processing_image") if isinstance(payload, dict) else None
        )
        if task.claimed_at is None:
            if image:
                return (
                    f"The task was never claimed by the worker — most often because "
                    f"the image ({image}) has no manifest for the worker's CPU "
                    f"architecture (it is amd64-only), which the claim guard "
                    f"refuses. Build/push the image for the worker's arch "
                    f"(or a worker matching its arch), then re-run."
                )
            return (
                "The task was never claimed by the worker. Check worker "
                "connectivity and Harbor credentials so the benchmark image "
                "can be pulled."
            )
        # Claimed and ran but did not finish in the window: the container
        # either hung or the pull/launch failed — point at the worker log.
        if image:
            return (
                f"The task was claimed but did not complete within the window "
                f"for image {image} — check the worker log for a pull/launch/"
                f"runtime error (Harbor credentials, worker connectivity)."
            )
        return "The task was claimed but did not complete within the window."

    def _expire_stuck_profiling_runs(self, db: Session):
        """Force-fail profiling runs that exceed PROFILING_MAX_RUNTIME_MINUTES.

        Profiling is a self-contained flow: one WorkflowRun with
        `parameters.is_profiling = true` and N benchmark tasks. If any of
        the tasks can't make progress (image-pull 401, worker never claims,
        benchmark container hangs past its per-container timeout, etc.),
        the run would otherwise sit as RUNNING forever and the dashboard
        keeps showing 'Queued — waiting for available worker'.

        On expiry: mark the run FAILED, cancel any of its still-open tasks,
        and mark the associated HardwareProfile benchmark_status='failed'
        with a human-readable error_message so the UI can surface it.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=PROFILING_MAX_RUNTIME_MINUTES)

        stuck = db.query(WorkflowRun).filter(
            WorkflowRun.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
            WorkflowRun.parameters["is_profiling"].as_boolean() == True,
            WorkflowRun.started_at < cutoff,
        ).all()

        if not stuck:
            return

        for run in stuck:
            age_min = (datetime.now(timezone.utc) - run.started_at).total_seconds() / 60.0
            logger.warning(
                "Profiling run %d exceeded %d-minute deadline (age=%.1fm); force-failing",
                run.id, PROFILING_MAX_RUNTIME_MINUTES, age_min,
            )

            run.status = TaskStatus.FAILED
            run.completed_at = datetime.now(timezone.utc)

            open_tasks = db.query(ApiTaskQueue).filter(
                ApiTaskQueue.workflow_run_id == run.id,
                ApiTaskQueue.status.in_([
                    ApiTaskStatus.PENDING,
                    ApiTaskStatus.ASSIGNED,
                    ApiTaskStatus.CLAIMED,
                    ApiTaskStatus.RUNNING,
                ]),
            ).all()
            for t in open_tasks:
                t.status = ApiTaskStatus.FAILED
                t.error_message = (
                    f"Profiling deadline exceeded ({PROFILING_MAX_RUNTIME_MINUTES} min). "
                    f"{self._profiling_task_failure_reason(t)}"
                )

            hp_id = run.parameters.get("hardware_profile_id") if run.parameters else None
            if hp_id:
                hp = db.query(HardwareProfile).filter(HardwareProfile.id == hp_id).first()
                if hp and hp.benchmark_status in ("pending", "running"):
                    hp.benchmark_status = "failed"
                    # HardwareProfile has no error_message column; pack the
                    # failure reason into benchmark_results so the dashboard
                    # can surface it next to the "failed" badge. Surface the
                    # reason for whichever task never made progress (prefer a
                    # never-claimed one — the real cause — over a claimed one).
                    unclaimed = [t for t in open_tasks if t.claimed_at is None]
                    culprit = unclaimed[0] if unclaimed else (open_tasks[0] if open_tasks else None)
                    reason = self._profiling_task_failure_reason(culprit) if culprit else (
                        "Most common cause: Harbor credentials not configured "
                        "(Settings → Secrets) so the benchmark image can't be pulled."
                    )
                    existing = dict(hp.benchmark_results or {})
                    existing["error"] = (
                        f"Profiling timed out after {PROFILING_MAX_RUNTIME_MINUTES} minutes. "
                        f"{reason}"
                    )
                    hp.benchmark_results = existing

        db.commit()

    def _handle_offline_workers(self, db: Session):
        """Reassign tasks from workers that have gone offline."""
        heartbeat_cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=15 * OFFLINE_HEARTBEAT_THRESHOLD
        )

        # Tombstoned (deleted_at set) workers are included regardless of
        # status/heartbeat: their unpinned ASSIGNED/CLAIMED tasks must be
        # requeued or they are stranded forever. Only their tasks are touched;
        # the worker row itself is never modified here.
        offline_workers = db.query(WorkerLicense).filter(
            or_(
                and_(
                    WorkerLicense.status == WorkerStatus.LICENSED,
                    WorkerLicense.last_heartbeat < heartbeat_cutoff,
                ),
                WorkerLicense.deleted_at.isnot(None),
            ),
        ).all()

        changed = False
        for w in offline_workers:
            # Reassign ASSIGNED and CLAIMED tasks
            orphaned = db.query(ApiTaskQueue).filter(
                ApiTaskQueue.status.in_([ApiTaskStatus.ASSIGNED, ApiTaskStatus.CLAIMED]),
                ApiTaskQueue.assigned_to_worker == w.worker_name,
            ).all()

            for task in orphaned:
                # Durable-pin exception (benchmark / host-pinned stack): keep
                # the pin. A hardware benchmark is only valid on the pinned
                # machine, and an operator's stack host pin must hold for the
                # whole life of the stack's tasks — so an offline pinned
                # worker must NOT re-pool the task for another box to claim.
                # It holds ASSIGNED until the worker returns (benchmarks: or
                # the profiling-deadline expirer force-fails the run).
                from utils.task_pinning import durable_target_worker
                if durable_target_worker(task):
                    logger.warning(
                        "Holding durably-pinned task %d on offline worker %s "
                        "(not reassigned — the pin is only valid on that host)",
                        task.id, w.worker_name,
                    )
                    task.claimed_by_worker = None
                    changed = True
                    continue
                logger.warning("Reassigning task %d from offline worker %s", task.id, w.worker_name)
                task.status = ApiTaskStatus.PENDING
                task.assigned_to_worker = None
                task.claimed_by_worker = None
                task.scheduling_version = (task.scheduling_version or 0) + 1
                changed = True

        # Commit only when a row was actually mutated; tombstoned workers
        # with nothing to requeue would otherwise commit every tick.
        if changed:
            db.commit()
