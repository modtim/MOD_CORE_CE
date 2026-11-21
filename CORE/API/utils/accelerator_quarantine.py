"""Accelerator quarantine — classify benchmark failures and derive which of a
worker's queues must be withheld because the accelerator behind them is dead.

PRINCIPLE: "a broken GPU can't keep the system from going." An accelerator that
cannot be benchmarked in its environment (e.g. an Intel GPU under WSL2 where
SYCL sees 0 devices because there is no /dev/dri) must be QUARANTINED
gracefully — the worker stays fully healthy and usable on its remaining
accelerators and CPU, and no task ever routes to the dead device.

Two jobs live here so the classifier and the scheduler never disagree:

  1. classify_accelerator_failure() — split a benchmark failure into a terminal
     'unsupported' (the runtime cannot see the device AT ALL in this
     environment; retrying re-runs the same probe and re-learns the same fact)
     versus a plain 'failed' (a transient or real error a bounded retry can
     still recover).

  2. quarantined_queues() — the set of queue names the claim path must drop for
     a worker because every device of that accelerator kind is dead. The queue
     the worker self-reports each heartbeat is advisory; this makes the
     exclusion hold server-side regardless of what the box advertises.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Set

logger = logging.getLogger(__name__)

# benchmark_status values in which a device CANNOT take work.
UNSCHEDULABLE_STATUSES = ("failed", "unsupported")

# Terminal, environment-level failures. The accelerator runtime cannot enumerate
# the device in this environment — a missing /dev/dri under WSL2, SYCL/Level-Zero
# finding zero devices, the xe/i915 kernel module not loaded. These are not
# transient: the same probe run again returns the same "no device" verdict, so
# they quarantine immediately instead of burning the bounded retry budget.
_UNSUPPORTED_PATTERNS = [
    r"sycl\s+found\s+0\s+(gpu\s+)?devices",
    r"\b0\s+(gpu\s+|opencl\s+|sycl\s+)?devices?\s+(found|available|detected)",
    r"no\s+(gpu|opencl|sycl|level[\s-]?zero|xpu|compatible)\s+devices?",
    r"no\s+devices?\s+were\s+found",
    r"/dev/dri",
    r"level[\s-]?zero.*(can'?t|cannot|could\s+not|failed\s+to|unable\s+to)\s+open",
    r"ze_result_error_device_lost",
    r"ze_result_error_uninitialized",
    r"\b(xe|i915)\b.*(not\s+loaded|missing|no\s+such|failed\s+to\s+load)",
    r"could\s+not\s+(load|find|open).*(xe|i915|level[\s-]?zero)",
    r"no\s+such\s+device",
    r"device\s+not\s+found",
    r"no\s+cuda[- ]capable\s+device",
    # AMD ROCm / HIP: the HIP runtime cannot enumerate a ROCm-capable device in
    # this environment (ROCm compute is unavailable under WSL2, the amdgpu KFD
    # node is missing, etc.). Terminal, same as the SYCL/CUDA no-device verdicts.
    # NB: "rocm" (with the m) is required so a working-device error like
    # "rocBLAS status invalid value" stays a plain retryable 'failed'.
    r"no\s+rocm[\s-]?capable\s+device",
    r"no\s+hip[\s-]?capable\s+device",
    r"hip\s+error\s+100\b",
    r"hiperrornodevice",
    r"hsa_status_error",
    r"rocm[\s\S]{0,40}?\bnot\b[\s\S]{0,20}?(detect|found|available)",
]
_UNSUPPORTED_RE = re.compile("|".join(_UNSUPPORTED_PATTERNS), re.IGNORECASE)


def classify_accelerator_failure(error_message: Optional[str]) -> str:
    """Return 'unsupported' when the error says the device is fundamentally
    absent in this environment (terminal quarantine), else 'failed' (a
    transient/real error a bounded retry can still recover)."""
    if error_message and _UNSUPPORTED_RE.search(error_message):
        return "unsupported"
    return "failed"


def is_unsupported_environment(error_message: Optional[str]) -> bool:
    """True when the failure is a terminal device-not-found / unsupported-
    environment error (so the caller can skip the retry budget entirely)."""
    return classify_accelerator_failure(error_message) == "unsupported"


def is_schedulable(accel) -> bool:
    """A device may receive work only when it is neither operator-disabled nor
    failed/unsupported."""
    if getattr(accel, "admin_disabled", False):
        return False
    return getattr(accel, "benchmark_status", None) not in UNSCHEDULABLE_STATUSES


def quarantined_queues(db, hardware_profile_id) -> Set[str]:
    """Uppercased queue names to WITHHOLD from a worker because every device of
    that accelerator kind is dead (failed / unsupported / admin-disabled).

    Queue name == accelerator kind uppercased (CUDA, INTEL, NPU, ROCM), the same
    convention _auto_dispatch_profiling and the worker's SupportedQueues() use.
    A kind is quarantined only when it has ≥1 device AND none of them is
    schedulable, so a partially-working kind (one dead card of several) still
    gets work and the per-device VRAM fit guard sorts out placement. CPU is
    never quarantined here — a worker always keeps its CPU queue.
    """
    if not hardware_profile_id:
        return set()
    import models
    rows = (
        db.query(
            models.AcceleratorProfile.kind,
            models.AcceleratorProfile.benchmark_status,
            models.AcceleratorProfile.admin_disabled,
        )
        .filter(models.AcceleratorProfile.hardware_profile_id == hardware_profile_id)
        .all()
    )
    by_kind: dict[str, list[bool]] = {}
    for kind, status, disabled in rows:
        k = (kind or "").strip().lower()
        if not k or k == "cpu":
            continue
        dead = bool(disabled) or status in UNSCHEDULABLE_STATUSES
        by_kind.setdefault(k, []).append(dead)
    return {k.upper() for k, deads in by_kind.items() if deads and all(deads)}


def filter_worker_queues(db, hardware_profile_id, queues) -> list:
    """Drop any queue whose accelerator kind is fully quarantined for this
    worker. Order-preserving; CPU and any queue with a live device survive.

    sz-build-queue.2: also drops the BUILD queue (platform-wide, independent
    of hardware_profile_id) when worker.build_queue_enabled is False — the
    same "withhold a queue name from the claim attempt server-side" job this
    function already does for a dead accelerator, reused so build tasks can
    be paused without touching the claim SQL in routers/worker_tasks.py."""
    dropped = quarantined_queues(db, hardware_profile_id)
    try:
        from services.build_queue_settings import (
            BUILD_QUEUE_NAME,
            build_queue_enabled,
        )
        if not build_queue_enabled(db=db):
            dropped = set(dropped) | {BUILD_QUEUE_NAME}
    except Exception:  # noqa: BLE001 — a settings-read failure must never
        # block ordinary claiming; proceed without the build-queue gate.
        logger.debug("build-queue capability gate check failed (proceeding)")
    if not dropped:
        return list(queues)
    return [q for q in queues if str(q).strip().upper() not in dropped]
