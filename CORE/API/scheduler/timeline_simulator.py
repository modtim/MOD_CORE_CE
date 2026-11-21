"""
Timeline Simulator — projects resource availability over time.

Given a worker's current state, running tasks, and assigned tasks,
simulates future resource availability at any point in time.
Immutable design: simulate_task() returns a NEW simulator for
exploring multiple placements without mutation.
"""

import copy
from product_names import themed_logger
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from scheduler.contention_model import (
    MemoryPressure,
    effective_nic_throughput,
    effective_vram,
    thermal_cpu_factor,
)

logger = themed_logger(__name__, 'scheduler')


@dataclass
class ResourceSnapshot:
    ram_available_mb: float = 0
    vram_available_mb: float = 0
    cpu_available: float = 0
    nic_available_mbps: float = 0
    disk_available_iops: float = 0
    gpu_idle: bool = True
    # MAC.1.s9b — what is left of a Metal worker's ONE RAM ∪ VRAM pool at
    # this instant. None on a discrete-GPU worker: there is no single pool,
    # RAM and VRAM remain two devices.
    shared_available_mb: Optional[float] = None


@dataclass
class PlannedTask:
    task_id: int
    node_kind: str
    start_offset_s: float
    processing_duration_s: float
    upload_duration_s: float
    download_duration_s: float
    peak_ram_mb: float
    peak_vram_mb: float
    peak_cpu_pct: float
    peak_net_mbps: float
    pattern_type: str
    is_gpu_bound: bool = False
    is_single_threaded: bool = False

    @property
    def total_duration_s(self) -> float:
        return self.download_duration_s + self.processing_duration_s + self.upload_duration_s

    @property
    def end_offset_s(self) -> float:
        return self.start_offset_s + self.total_duration_s

    def phase_at(self, time_s: float) -> Optional[str]:
        t = time_s - self.start_offset_s
        if t < 0 or t > self.total_duration_s:
            return None
        if t < self.download_duration_s:
            return "download"
        if t < self.download_duration_s + self.processing_duration_s:
            return "processing"
        return "upload"

    def resources_at(self, time_s: float) -> ResourceSnapshot:
        phase = self.phase_at(time_s)
        if phase is None:
            return ResourceSnapshot()
        if phase == "download":
            return ResourceSnapshot(
                ram_available_mb=-self.peak_ram_mb * 0.2,
                nic_available_mbps=-self.peak_net_mbps,
            )
        if phase == "processing":
            return ResourceSnapshot(
                ram_available_mb=-self.peak_ram_mb,
                vram_available_mb=-self.peak_vram_mb,
                cpu_available=-self.peak_cpu_pct / 100.0,
                gpu_idle=not self.is_gpu_bound,
            )
        if phase == "upload":
            return ResourceSnapshot(
                ram_available_mb=-self.peak_ram_mb * 0.3,
                nic_available_mbps=-self.peak_net_mbps,
            )
        return ResourceSnapshot()


class TimelineSimulator:
    """Immutable resource timeline projection for a single worker."""

    def __init__(
        self,
        total_ram_mb: float,
        total_vram_mb: float,
        cpu_cores: int,
        nic_capacity_mbps: float,
        tasks: List[PlannedTask] = None,
        shared_pool_mb: Optional[float] = None,
    ):
        self.total_ram_mb = total_ram_mb
        self.total_vram_mb = total_vram_mb
        self.cpu_cores = cpu_cores
        self.nic_capacity_mbps = nic_capacity_mbps
        self.ram_ceiling_mb = MemoryPressure.effective_ram_ceiling(total_ram_mb)
        self.tasks = list(tasks or [])

        # MAC.1.s9b — shared-pool mode for Metal (unified-memory) workers.
        # The caller passes the pool the overlap scheduler already computed
        # (max(ram_total, vram_total) — never the sum); passing it here
        # instead of re-detecting keeps ONE detector, overlap_scheduler's
        # _is_unified_memory_worker. None / 0 / a pool that doesn't exceed
        # RAM means two separate devices: every number below stays exactly
        # as the CUDA/ROCm path always had it.
        # A Metal box's probe puts the SAME unified figure in ram_gb and
        # vram_gb, so the pool legitimately EQUALS max(ram, vram) — the only
        # thing that means "two devices" is the caller passing no pool at all
        # (discrete CUDA/ROCm workers, whose call sites are unchanged).
        self.shared_pool_mb = float(shared_pool_mb or 0.0)
        self.shared_pool = self.shared_pool_mb > 0.0
        self.shared_ceiling_mb = (
            MemoryPressure.effective_ram_ceiling(self.shared_pool_mb)
            if self.shared_pool else 0.0
        )

    def available_at(self, time_s: float) -> ResourceSnapshot:
        ram_used = 0.0
        vram_used = 0.0
        cpu_used = 0.0
        nic_used = 0.0
        gpu_idle = True

        for task in self.tasks:
            res = task.resources_at(time_s)
            ram_used -= res.ram_available_mb
            vram_used -= res.vram_available_mb
            cpu_used -= res.cpu_available
            nic_used -= res.nic_available_mbps
            if not res.gpu_idle:
                gpu_idle = False

        snapshot = ResourceSnapshot(
            ram_available_mb=self.ram_ceiling_mb - ram_used,
            vram_available_mb=self.total_vram_mb - vram_used,
            cpu_available=float(self.cpu_cores) - cpu_used,
            nic_available_mbps=self.nic_capacity_mbps - nic_used,
            gpu_idle=gpu_idle,
        )
        if self.shared_pool:
            # MAC.1.s9b — ONE pool: every task's RAM+VRAM demand is counted
            # ONCE against it. `resources_at` reports demand as negative
            # availability, so summing the two negatives per task gives that
            # task's combined demand; the pool sees it once, never as two
            # separate bookings.
            shared_used = 0.0
            for task in self.tasks:
                res = task.resources_at(time_s)
                shared_used -= (res.ram_available_mb + res.vram_available_mb)
            snapshot.shared_available_mb = self.shared_ceiling_mb - shared_used
        return snapshot

    def can_fit_task(self, task: PlannedTask, start_at_s: float) -> Tuple[bool, float]:
        check_points = [
            start_at_s,
            start_at_s + task.download_duration_s,
            start_at_s + task.download_duration_s + task.processing_duration_s * 0.5,
            start_at_s + task.download_duration_s + task.processing_duration_s,
        ]

        for t in check_points:
            avail = self.available_at(t)
            task_res = task.resources_at(t)

            if self.shared_pool:
                # MAC.1.s9b — the shared pool is the ONLY gate: the two
                # device checks below would double-book one physical pool
                # (a Metal box's vram_gb is the same unified figure, not a
                # card). Demand = RAM + VRAM, counted once.
                if avail.shared_available_mb is not None and \
                        avail.shared_available_mb + task_res.ram_available_mb \
                        + task_res.vram_available_mb < 0:
                    earliest = self._find_earliest_fit(task, start_at_s)
                    return False, earliest
                continue
            if avail.ram_available_mb + task_res.ram_available_mb < 0:
                earliest = self._find_earliest_fit(task, start_at_s)
                return False, earliest
            if task.peak_vram_mb > 0 and avail.vram_available_mb + task_res.vram_available_mb < 0:
                earliest = self._find_earliest_fit(task, start_at_s)
                return False, earliest

        return True, start_at_s

    def _find_earliest_fit(self, task: PlannedTask, after_s: float) -> float:
        for offset in range(int(after_s), int(after_s) + 3600, 5):
            fits, _ = self._check_fit_at(task, float(offset))
            if fits:
                return float(offset)
        return after_s + 3600

    def _check_fit_at(self, task: PlannedTask, start_at_s: float) -> Tuple[bool, float]:
        t_mid = start_at_s + task.download_duration_s + task.processing_duration_s * 0.5
        avail = self.available_at(t_mid)
        if self.shared_pool:
            # MAC.1.s9b — same single-pool rule for the earliest-fit scan.
            fits = (avail.shared_available_mb is not None
                    and avail.shared_available_mb
                    > task.peak_ram_mb + task.peak_vram_mb)
            return (fits, start_at_s)
        if avail.ram_available_mb > task.peak_ram_mb:
            if task.peak_vram_mb == 0 or avail.vram_available_mb > task.peak_vram_mb:
                return True, start_at_s
        return False, start_at_s

    def simulate_task(self, task: PlannedTask) -> 'TimelineSimulator':
        new_sim = TimelineSimulator(
            total_ram_mb=self.total_ram_mb,
            total_vram_mb=self.total_vram_mb,
            cpu_cores=self.cpu_cores,
            nic_capacity_mbps=self.nic_capacity_mbps,
            tasks=self.tasks + [task],
            # MAC.1.s9b — shared-pool mode must survive every simulate_task
            # hop, or the 2-opt / lookahead passes silently fall back to
            # double-booking the pool after the first placement.
            shared_pool_mb=self.shared_pool_mb,
        )
        return new_sim

    def makespan(self) -> float:
        if not self.tasks:
            return 0.0
        return max(t.end_offset_s for t in self.tasks)

    def utilization(self, duration_s: float = None) -> float:
        if not self.tasks:
            return 0.0
        duration = duration_s or self.makespan()
        if duration == 0:
            return 0.0

        total_work = sum(t.total_duration_s for t in self.tasks)
        return min(1.0, total_work / (duration * max(1, len(self.tasks))))

    def gpu_idle_windows(self) -> List[Tuple[float, float]]:
        if not self.tasks:
            return [(0, float('inf'))]

        windows = []
        end = self.makespan()
        step = 5.0
        t = 0.0
        window_start = None

        while t <= end:
            avail = self.available_at(t)
            if avail.gpu_idle:
                if window_start is None:
                    window_start = t
            else:
                if window_start is not None:
                    windows.append((window_start, t))
                    window_start = None
            t += step

        if window_start is not None:
            windows.append((window_start, end))

        return windows

    def complementary_score(self, task: PlannedTask) -> float:
        if not self.tasks:
            return 0.5

        existing_gpu_bound = any(t.is_gpu_bound for t in self.tasks
                                 if t.phase_at(task.start_offset_s + task.processing_duration_s * 0.5) == "processing")

        if task.is_gpu_bound and not existing_gpu_bound:
            return 1.0
        if not task.is_gpu_bound and existing_gpu_bound:
            return 1.0
        if task.is_single_threaded and existing_gpu_bound:
            return 0.9

        return 0.3
