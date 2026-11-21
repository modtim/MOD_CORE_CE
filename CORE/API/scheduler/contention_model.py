"""
Contention Model — models non-linear resource contention effects.

Resources don't scale linearly when shared. This module captures the
real-world degradation curves for NIC bandwidth, Docker daemon throughput,
disk I/O, memory pressure, GPU contexts, and other contention factors.
"""

import math


# =============================================================================
# NIC Contention
# =============================================================================

def effective_nic_throughput(nic_capacity_mbps: float, concurrent_transfers: int) -> float:
    """
    When N transfers share a NIC, each gets less than capacity/N due to
    TCP congestion control oscillation and protocol overhead.

    Returns effective throughput per transfer in Mbps.
    """
    if concurrent_transfers <= 0:
        return nic_capacity_mbps
    if concurrent_transfers == 1:
        return nic_capacity_mbps * 0.92  # Single-flow ~92% efficiency

    # Efficiency drops with concurrency: 0.85^log2(N) approximation
    efficiency = 0.85 ** math.log2(concurrent_transfers)
    per_transfer = (nic_capacity_mbps * efficiency) / concurrent_transfers
    return max(per_transfer, 1.0)  # Floor at 1 Mbps


def total_nic_utilization(nic_capacity_mbps: float, transfer_rates_mbps: list) -> float:
    """
    Given a list of current transfer rates, compute NIC utilization as a fraction [0, 1].
    Accounts for protocol overhead (each transfer adds ~5% overhead).
    """
    if not transfer_rates_mbps or nic_capacity_mbps <= 0:
        return 0.0

    raw_total = sum(transfer_rates_mbps)
    overhead = len(transfer_rates_mbps) * 0.05 * nic_capacity_mbps
    effective_total = raw_total + overhead
    return min(effective_total / nic_capacity_mbps, 1.0)


# =============================================================================
# Docker Daemon Contention
# =============================================================================

# Container create/start is serialized through containerd.
# Empirical timing: ~2s per operation on average hardware.
DOCKER_OP_SECONDS = 2.0
DOCKER_MAX_CONCURRENT_OPS = 5  # Beyond this, queueing delay is significant


def docker_operation_delay(active_docker_ops: int) -> float:
    """
    Estimate additional delay (in seconds) for a new Docker create/start
    operation given the current number of in-flight operations.

    Returns 0 if no contention, otherwise the estimated queue wait time.
    """
    if active_docker_ops < DOCKER_MAX_CONCURRENT_OPS:
        return 0.0

    # Queueing model: each additional op beyond the limit adds ~2s wait
    queue_depth = active_docker_ops - DOCKER_MAX_CONCURRENT_OPS
    return queue_depth * DOCKER_OP_SECONDS


# =============================================================================
# Disk I/O Contention
# =============================================================================

def disk_throughput_factor(concurrent_random_io: int, concurrent_sequential_io: int) -> float:
    """
    When random I/O (container starts, layer extraction) and sequential I/O
    (downloads, uploads) compete, sequential throughput drops significantly.

    Returns a degradation factor [0.0, 1.0] to apply to sequential I/O estimates.
    """
    if concurrent_random_io <= 0:
        return 1.0  # No degradation

    # Random I/O causes head seeks that interrupt sequential reads
    # ~40% degradation per concurrent random source, diminishing
    degradation = 0.4 * (1.0 - math.exp(-concurrent_random_io))
    return max(1.0 - degradation, 0.2)


# =============================================================================
# Memory Pressure Zones
# =============================================================================

class MemoryPressure:
    GREEN = "green"    # 0-70%: No impact
    YELLOW = "yellow"  # 70-85%: Filesystem cache evicted, I/O slows
    RED = "red"        # 85%+: Swapping risk, refuse placement

    # I/O slowdown factor in each zone
    IO_FACTORS = {GREEN: 1.0, YELLOW: 0.8, RED: 0.5}

    @staticmethod
    def classify(ram_used_mb: float, ram_total_mb: float) -> str:
        if ram_total_mb <= 0:
            return MemoryPressure.GREEN
        pct = ram_used_mb / ram_total_mb
        if pct >= 0.85:
            return MemoryPressure.RED
        elif pct >= 0.70:
            return MemoryPressure.YELLOW
        return MemoryPressure.GREEN

    @staticmethod
    def effective_ram_ceiling(ram_total_mb: float) -> float:
        """The hard ceiling: scheduler never plans past 85% of total RAM."""
        return ram_total_mb * 0.85

    @staticmethod
    def io_factor(zone: str) -> float:
        """I/O throughput degradation factor for the given memory pressure zone."""
        return MemoryPressure.IO_FACTORS.get(zone, 1.0)


# =============================================================================
# GPU Memory Contention
# =============================================================================

# Each CUDA context has ~200MB overhead for driver structures
GPU_CONTEXT_OVERHEAD_MB = 200.0


def effective_vram(vram_total_mb: float, concurrent_gpu_tasks: int,
                   fragmentation_pct: float = 0.0) -> float:
    """
    Compute effective available VRAM accounting for:
    - Per-context overhead (200MB each)
    - Fragmentation (largest contiguous block may be smaller than total free)
    - 15% safety buffer for contiguous allocation
    """
    context_overhead = concurrent_gpu_tasks * GPU_CONTEXT_OVERHEAD_MB
    available = vram_total_mb - context_overhead

    # Apply fragmentation: reduce available by fragmentation percentage
    if fragmentation_pct > 0:
        available *= (1.0 - fragmentation_pct)

    # 15% safety buffer for contiguous allocation
    available *= 0.85

    return max(available, 0.0)


# =============================================================================
# Log Streaming Load
# =============================================================================

def log_streaming_bandwidth_mbps(high_output_containers: int) -> float:
    """
    High-output containers (>1MB/s logs) consume Redis connection bandwidth.
    Returns estimated bandwidth in Mbps used for log streaming.
    """
    # ~5 Mbps per high-output container
    return high_output_containers * 5.0


# =============================================================================
# GC Pressure (Go Worker)
# =============================================================================

def gc_pause_overhead_seconds(active_containers: int) -> float:
    """
    Go worker GC pauses increase with heap size (which grows with active containers).
    Returns estimated per-operation overhead in seconds.

    Below 10 containers: negligible
    10-20 containers: ~10ms per operation
    20+ containers: ~50ms per operation
    """
    if active_containers < 10:
        return 0.0
    elif active_containers < 20:
        return 0.01
    else:
        return 0.05


# =============================================================================
# Cross-Region Latency
# =============================================================================

# Number of API round-trips per task lifecycle
TASK_API_ROUNDTRIPS = 6  # validate(×phases) + started + samples(×N) + complete

# TLS handshake overhead for first connection
TLS_HANDSHAKE_MS = 50
DNS_COLD_CACHE_MS = 100


def remote_worker_overhead_seconds(api_latency_ms: float, is_first_connection: bool = False) -> float:
    """
    Compute total overhead for a remote worker executing a task.
    Includes API round-trips, TLS handshake, and DNS resolution.
    """
    base = (api_latency_ms * TASK_API_ROUNDTRIPS) / 1000.0

    if is_first_connection:
        base += (TLS_HANDSHAKE_MS + DNS_COLD_CACHE_MS) / 1000.0

    return base


# =============================================================================
# Thermal Throttling
# =============================================================================

def thermal_cpu_factor(cpu_temp_c: float, throttled: bool) -> float:
    """
    CPU performance degradation due to thermal throttling.
    Returns a factor [0.0, 1.0] to apply to CPU capacity estimates.
    """
    if throttled:
        return 0.7  # Typical thermal throttle reduces to ~70% performance

    # Progressive degradation as temperature approaches throttle point (~95C)
    if cpu_temp_c > 90:
        return 0.85
    elif cpu_temp_c > 80:
        return 0.95
    return 1.0


# =============================================================================
# Image Layer Extraction
# =============================================================================

def image_mount_seconds(image_size_gb: float) -> float:
    """
    Even cached images need layer extraction to a new container's filesystem.
    Large ML images (10GB+) take noticeable time.
    """
    if image_size_gb <= 1:
        return 1.0
    elif image_size_gb <= 5:
        return 2.0
    elif image_size_gb <= 10:
        return 4.0
    else:
        return 4.0 + (image_size_gb - 10) * 0.5


# =============================================================================
# Shared (Unified) Memory Pool Contention — MAC.1.s9c
# =============================================================================


def shared_pool_contention(
    pool_mb: float,
    demands_mb: list,
    ceiling_pct: float = 0.85,
) -> dict:
    """
    Contention signal for a Metal / Apple-Silicon worker's ONE RAM ∪ VRAM
    pool. Such a box has no separate GPU memory: a task that wants 6 GB of
    "VRAM" and a task that wants 6 GB of "RAM" are fighting for the same
    physical pages, so the pool can be oversubscribed by demands that each
    look fine against RAM and against VRAM on their own.

    A single Metal worker can never be oversubscribed against ITSELF: the
    scheduler must see this signal and refuse/defer the placement, not
    assume the two device pools will absorb it.

    Returns a dict:
      oversubscribed : bool  — demands exceed the usable pool (85% ceiling)
      used_mb        : float — combined demand counted ONCE per task
      available_mb   : float — what the pool still has under the ceiling
      pressure       : str   — MemoryPressure zone for the pool
      ratio          : float — used / pool (can exceed 1.0 when oversubscribed)
      pool_mb        : float — the pool size as given

    Discrete-GPU (CUDA/ROCm) workers never call this: they have two real
    devices and their two-pool maths is untouched. A non-positive pool
    returns an inert all-clear result so callers can pass either kind.
    """
    inert = {
        "oversubscribed": False,
        "used_mb": 0.0,
        "available_mb": 0.0,
        "pressure": MemoryPressure.GREEN,
        "ratio": 0.0,
        "pool_mb": 0.0,
    }
    pool = float(pool_mb or 0.0)
    if pool <= 0:
        return inert

    # One demand number per task: RAM + VRAM counted ONCE (callers pass the
    # combined figure; nothing here ever adds a second copy).
    used = sum(float(d or 0.0) for d in (demands_mb or []))
    ceiling = pool * ceiling_pct
    available = ceiling - used
    return {
        "oversubscribed": available < 0,
        "used_mb": used,
        "available_mb": available,
        "pressure": MemoryPressure.classify(used, pool),
        "ratio": used / pool,
        "pool_mb": pool,
    }


def unified_memory_pool_mb(worker) -> float:
    """
    The ONE pool size for a Metal worker: max(ram_total, vram_total), NEVER
    the sum. The MAC.1.s7 probe reports the unified figure in both fields
    (or leaves vram_gb absent), so summing them would invent a second,
    non-existent device and double the budget.

    Detection is delegated to overlap_scheduler's single helper so there is
    exactly one Metal detector in the platform; 0.0 means "not a shared-pool
    worker" (discrete CUDA/ROCm box) and every caller keeps its old path.
    """
    # Deferred import: overlap_scheduler imports this module at module
    # scope, so importing it back here would be a circular import.
    from scheduler.overlap_scheduler import _is_unified_memory_worker

    if not _is_unified_memory_worker(worker):
        return 0.0
    hw = getattr(worker, "hardware_specs", None) or {}
    ram_mb = float(hw.get("ram_gb", 0) or 0) * 1024
    vram_mb = float(hw.get("vram_gb", 0) or 0) * 1024
    return max(ram_mb, vram_mb)


# =============================================================================
# GPU Power State Warmup
# =============================================================================

def gpu_warmup_seconds(current_power_state: str) -> float:
    """
    GPU power state transition time.
    P0 (active) → 0s, P5-P8 (idle) → 2s, P12 (low_power) → 5s.
    """
    if current_power_state == "active":
        return 0.0
    elif current_power_state == "idle":
        return 2.0
    elif current_power_state == "low_power":
        return 5.0
    return 1.0  # Unknown state, assume small warmup
