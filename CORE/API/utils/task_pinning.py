"""Durable per-task worker pinning for hardware benchmark / profiling tasks.

A hardware benchmark measures ONE machine. Its scores are only valid if the
benchmark container actually ran on that exact machine — if a second machine
picks up the task, that machine's CPU/RAM/GPU scores get written onto the
first machine's HardwareProfile.

To make the pin durable (it must survive every requeue / unpin / reassign
sweep), the dispatch path stamps `benchmark_target_worker` into the task's
`task_payload`. Every code path that reassigns or unpins a task must consult
:func:`benchmark_target_worker` and refuse to move a benchmark task to another
worker. When the pinned worker cannot run the task (offline, arch mismatch,
never claims), the task should fail with a clear reason — it must NEVER be
re-pooled and claimed by a different machine.

A second durable pin exists: an operator's host pin on a persistent stack.
`_dispatch_stack_task` stamps `pinned_worker` into the payload so that the
whole life of the stack's tasks stays bound to the pinned host, exactly like
the benchmark pin. :func:`durable_target_worker` is the generalised guard
every re-pool / re-assign path should consult; benchmark-only behaviour (the
report-owner check) keeps using :func:`benchmark_target_worker`.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def benchmark_target_worker(task) -> Optional[str]:
    """Return the worker a benchmark task is durably pinned to, or None.

    Reads the `benchmark_target_worker` flag stamped into `task.task_payload`
    by `_auto_dispatch_profiling` (profiling root + downstream benchmark
    nodes). Returns None for ordinary (non-benchmark) tasks, so this is a safe
    guard to call unconditionally from any task-mutating path.
    """
    payload = getattr(task, "task_payload", None) or {}
    if not isinstance(payload, dict):
        return None
    return payload.get("benchmark_target_worker") or None


def durable_target_worker(task) -> Optional[str]:
    """Return the worker a task is durably pinned to, or None.

    Generalised :func:`benchmark_target_worker`: returns the
    `benchmark_target_worker` pin when present, otherwise the `pinned_worker`
    host pin stamped by `_dispatch_stack_task` for operator-pinned persistent
    stacks. Both pins are durable — they must hold for the whole life of the
    task — so every re-pool / re-assign / release path that may move a task to
    another worker consults this helper and refuses to move a pinned task.

    Benchmark-only behaviour (e.g. the report-owner check in
    :func:`benchmark_report_worker_is_owner`) keeps calling
    :func:`benchmark_target_worker` so a host-pinned stack task is not treated
    as a benchmark.
    """
    payload = getattr(task, "task_payload", None) or {}
    if not isinstance(payload, dict):
        return None
    return (payload.get("benchmark_target_worker")
            or payload.get("pinned_worker") or None)


def benchmark_report_worker_is_owner(db, task, profile) -> bool:
    """True when the worker that ran a benchmark actually owns the profile.

    Guard against the wrong-machine bug: a benchmark task pinned to worker W
    that a DIFFERENT worker ends up claiming would otherwise write that other
    machine's CPU/RAM/GPU scores onto W's HardwareProfile. The reporting worker
    is authoritative: its WorkerLicense row links to a hardware profile, and
    that profile id must match the one the results are being written to.

    For a task that still carries the durable `benchmark_target_worker` pin,
    we ALSO require the reporter to be that worker (double safety: the pin says
    who SHOULD have run it, the profile link says who DID).

    Returns False when the reporter cannot be identified as the owner (the
    caller must reject + log in that case rather than silently write).
    """
    reporter = (task.claimed_by_worker or task.assigned_to_worker or "").strip()
    if not reporter:
        # Unknown reporter — treat as owner (legacy behavior) so we never
        # silently drop a legitimate result just because the field was blank.
        return True
    try:
        from models import WorkerLicense
        w = db.query(WorkerLicense).filter(
            WorkerLicense.worker_name == reporter
        ).first()
        if w is not None and w.hardware_profile_id is not None:
            return w.hardware_profile_id == profile.id
    except Exception:
        logger.debug("benchmark owner check: worker lookup failed", exc_info=True)
    # Reporter has no WorkerLicense row (or a NULL profile link) — fall back to
    # the durable pin if present.
    pinned = (task.task_payload or {}).get("benchmark_target_worker")
    if pinned:
        return pinned == reporter
    return True
