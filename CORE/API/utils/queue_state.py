"""
Queue State Tracking with Redis - Phase 3c

Tracks real-time queue state for intelligent routing:
- Which workers are busy
- How long until each worker is free
- Queue depth per worker
- Running task ETAs

This enables "wait vs route" decisions:
- "Wait 5 seconds for RTX 4090" vs "Route to GTX 1080 now"

Redis Data Structures:
- worker:{worker_name}:current_task -> {task_id, started_at, estimated_duration}
- worker:{worker_name}:queue -> Sorted set of queued task IDs by priority
- task:{workflow_run_id}:eta -> {started_at, estimated_duration, predicted_by}
"""

import redis
import os
import json
import time
import atexit
import threading
from typing import Dict, Any, Optional, List, Callable
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# Telemetry batching (scale-tiered-workloads, decision D5)
# =============================================================================
# High-frequency worker telemetry — resource samples, pattern updates, and the
# queue-state heartbeats in THIS module — historically did one write per event.
# At studio scale (hundreds of concurrent tasks each streaming samples every
# 30s) the *write* rate, not the query cost, becomes the DB/Redis bottleneck
# (design.md D5). This primitive coalesces N per-event writes into a single
# flush of N items on a size threshold or a short timer, so the write rate is
# decoupled from the event rate.
#
# It is BEHIND A FLAG and DEFAULT OFF (MOD_TELEMETRY_BATCHING). With the flag
# unset (today's deployed state) every `add()` flushes immediately — i.e. the
# exact per-event write behaviour we have now — so shipping this changes nothing
# until an operator opts in. Data is never dropped: the buffer flushes on the
# size threshold, on the periodic timer, and on interpreter shutdown (atexit).
#
# The primitive itself has ZERO heavy dependencies (stdlib only) and takes the
# actual write as an injected `flush_fn(items)` callback, so each call site
# supplies its own storage (Postgres / Redis) without this module importing it.


def telemetry_batching_enabled() -> bool:
    """True iff the `telemetry_batching_enabled` platform setting is on
    (env fallback ``MOD_TELEMETRY_BATCHING``).

    Read live (not cached at import) so the flag can be toggled per-process at
    startup — and so tests can flip it via the environment. DEFAULT FALSE: an
    unset/empty/`0`/`false` value means "write per event exactly as today".
    """
    from services import platform_settings as _psr
    return bool(_psr.get_setting("telemetry_batching_enabled"))


class TelemetryBatcher:
    """Coalesce high-frequency telemetry writes into batched flushes (D5).

    When batching is DISABLED (default), every ``add()`` calls ``flush_fn`` with
    a single-item list immediately — byte-for-byte today's one-write-per-event
    behaviour. When ENABLED, items accumulate in an in-process buffer and are
    flushed as one ``flush_fn(items)`` call when either the buffer reaches
    ``max_batch_size`` or ``flush_interval_s`` elapses since the first buffered
    item, whichever comes first. ``flush_fn`` is invoked once per flush with the
    whole batch, so the caller writes N items in a single transaction / pipeline.

    Thread-safe: the API serves requests concurrently, so ``add()`` and the
    timer thread can race on the buffer; a lock guards it. ``flush_fn`` is called
    OUTSIDE the lock so a slow write never blocks producers.
    """

    def __init__(
        self,
        name: str,
        flush_fn: Callable[[list], None],
        *,
        max_batch_size: int = 100,
        flush_interval_s: float = 5.0,
    ):
        self.name = name
        self._flush_fn = flush_fn
        self._max_batch_size = max(1, int(max_batch_size))
        self._flush_interval_s = float(flush_interval_s)
        self._buffer: list = []
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        _register_batcher(self)

    def add(self, item: Any) -> None:
        """Record one telemetry event.

        Flag OFF → flush this one item now (1:1 with the event, unchanged).
        Flag ON  → buffer it; flush when the batch is full or the timer fires.
        """
        if not telemetry_batching_enabled():
            # Passthrough: identical to the pre-D5 per-event write path.
            self._safe_flush([item])
            return

        due = False
        with self._lock:
            self._buffer.append(item)
            if len(self._buffer) >= self._max_batch_size:
                due = True
            elif self._timer is None:
                # First item in an empty buffer — arm the periodic flush so a
                # low-volume trickle still lands within flush_interval_s and is
                # never stranded in memory.
                self._timer = threading.Timer(self._flush_interval_s, self.flush)
                self._timer.daemon = True
                self._timer.start()
        if due:
            self.flush()

    def flush(self) -> None:
        """Drain the buffer and write it as a single batch (safe to call any time)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if not self._buffer:
                return
            items = self._buffer
            self._buffer = []
        # flush_fn runs outside the lock: a slow DB write must not block add().
        self._safe_flush(items)

    def _safe_flush(self, items: list) -> None:
        if not items:
            return
        try:
            self._flush_fn(items)
        except Exception as e:  # never let a telemetry write take down a caller
            logger.error(
                "TelemetryBatcher[%s] flush of %d item(s) failed: %s",
                self.name, len(items), e,
            )


# Registry of live batchers so a single atexit hook flushes them all on
# shutdown — the "do not lose data" guarantee for buffered items in flight when
# the process is asked to stop (D5).
_BATCHERS: List[TelemetryBatcher] = []
_BATCHERS_LOCK = threading.Lock()


def _register_batcher(b: "TelemetryBatcher") -> None:
    with _BATCHERS_LOCK:
        _BATCHERS.append(b)


def flush_all_batchers() -> None:
    """Flush every registered batcher. Wired to atexit; also callable directly
    from a graceful-shutdown handler so no buffered telemetry is lost."""
    with _BATCHERS_LOCK:
        batchers = list(_BATCHERS)
    for b in batchers:
        try:
            b.flush()
        except Exception as e:
            logger.error("flush_all_batchers: %s failed: %s", b.name, e)


atexit.register(flush_all_batchers)

# Resolve the Redis URL for queue-state metadata. Priority:
#   1. QUEUE_STATE_REDIS_URL (explicit per-feature override).
#   2. Derive from the platform-wide REDIS_URL by swapping the DB to
#      /1 (queue-state keeps its own DB so its keys don't collide
#      with the general cache that lives in /2).
#   3. Sane default at the live hostname (`redis:6379`, alias for `my_redis`).
def _derive_queue_state_url() -> str:
    explicit = os.getenv("QUEUE_STATE_REDIS_URL")
    if explicit:
        return explicit
    base = os.getenv("REDIS_URL")
    if base:
        head, sep, _ = base.rpartition("/")
        if sep:
            return head + "/1"
        return base.rstrip("/") + "/1"
    return "redis://redis:6379/1"


REDIS_URL = _derive_queue_state_url()
redis_client = redis.from_url(REDIS_URL, decode_responses=True)


def scan_keys(pattern: str, count: int = 100) -> list:
    """
    Scan Redis keys matching a pattern using SCAN (non-blocking).

    Unlike KEYS which blocks Redis for the entire duration,
    SCAN iterates incrementally and is safe for production use.

    Args:
        pattern: Redis key pattern (e.g., "worker:*:current_task")
        count: Hint for number of keys to return per iteration

    Returns:
        List of matching keys
    """
    keys = []
    cursor = 0
    while True:
        cursor, batch = redis_client.scan(cursor=cursor, match=pattern, count=count)
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


def register_task_start(
    worker_name: str,
    workflow_run_id: int,
    node_id: str,
    estimated_duration_seconds: float,
    metadata: Optional[Dict[str, Any]] = None
):
    """
    Register that a task has started on a worker.

    Called by worker containers at the start of processing.

    Args:
        worker_name: Worker identifier (e.g., "gpu-server-1")
        workflow_run_id: Workflow run ID
        node_id: Node identifier
        estimated_duration_seconds: ML-predicted or historical average duration
        metadata: Optional additional info (file_size, node_kind, etc.)
    """
    started_at = time.time()
    item = {
        "worker_name": worker_name,
        "workflow_run_id": workflow_run_id,
        "node_id": node_id,
        "started_at": started_at,
        "estimated_duration": estimated_duration_seconds,
        "estimated_completion": started_at + estimated_duration_seconds,
        "metadata": metadata or {},
    }
    # D5: heartbeats are a high-frequency per-event write. Behind
    # MOD_TELEMETRY_BATCHING (default off) each start still writes immediately
    # (below is the exact pre-D5 code path); with batching on, starts coalesce
    # into a single Redis pipeline per flush.
    _START_BATCHER.add(item)


def _flush_task_starts(items: list) -> None:
    """Write a batch of task-start heartbeats in one Redis pipeline (D5).

    With the flag off this is called with a single-item list per event, so the
    two SETEX calls are byte-for-byte the original inline behaviour.
    """
    try:
        pipe = redis_client.pipeline(transaction=False)
        for item in items:
            est = item["estimated_duration"]
            task_data = {
                "workflow_run_id": item["workflow_run_id"],
                "node_id": item["node_id"],
                "started_at": item["started_at"],
                "estimated_duration": est,
                "estimated_completion": item["estimated_completion"],
                "metadata": item["metadata"],
            }
            ttl = int(est) + 3600  # TTL = estimate + 1 hour buffer
            payload = json.dumps(task_data)
            # Store current task for this worker + task ETA for lookups
            pipe.setex(f"worker:{item['worker_name']}:current_task", ttl, payload)
            pipe.setex(f"task:{item['workflow_run_id']}:eta", ttl, payload)
            logger.info(f"✅ Registered task start: {item['workflow_run_id']} on "
                        f"{item['worker_name']} (ETA: {est:.1f}s)")
        pipe.execute()
    except Exception as e:
        logger.error(f"Failed to register task start: {e}")


_START_BATCHER = TelemetryBatcher("queue_state.task_start", _flush_task_starts)


def register_task_complete(worker_name: str, workflow_run_id: int):
    """
    Register that a task has completed.

    Called by worker containers at the end of processing (success or failure).

    Args:
        worker_name: Worker identifier
        workflow_run_id: Workflow run ID
    """
    # D5: same batching treatment as register_task_start. Default-off path
    # writes immediately; batched path coalesces deletes into one pipeline.
    _COMPLETE_BATCHER.add({
        "worker_name": worker_name,
        "workflow_run_id": workflow_run_id,
    })


def _flush_task_completes(items: list) -> None:
    """Write a batch of task-complete heartbeats in one Redis pipeline (D5)."""
    try:
        pipe = redis_client.pipeline(transaction=False)
        for item in items:
            # Remove current task; the ETA key would expire naturally (TTL) but
            # we delete it early, exactly as before.
            pipe.delete(f"worker:{item['worker_name']}:current_task")
            pipe.delete(f"task:{item['workflow_run_id']}:eta")
            logger.info(f"✅ Registered task complete: {item['workflow_run_id']} "
                        f"on {item['worker_name']}")
        pipe.execute()
    except Exception as e:
        logger.error(f"Failed to register task complete: {e}")


_COMPLETE_BATCHER = TelemetryBatcher("queue_state.task_complete", _flush_task_completes)


def get_queue_depth(queue_name: str) -> int:
    """
    Per-queue depth lookup. The Go worker drains Postgres
    `ApiTaskQueue` rows directly; a real implementation would COUNT(*)
    pending rows for `queue_name`. Returns 0 today (callers treat all
    queues as empty) — wire this up when queue-time prediction
    actually starts gating routing decisions.
    """
    return 0


def get_worker_availability(worker_name: str) -> Dict[str, Any]:
    """
    Get worker availability information.

    Returns:
        dict: {
            'is_busy': bool,
            'current_task': dict or None,
            'estimated_free_in_seconds': float,
            'queue_depth': int
        }
    """
    try:
        # Check if worker has a current task
        current_task_json = redis_client.get(f"worker:{worker_name}:current_task")

        # Get queue depth for this worker's queues
        # Try to determine queue from worker metadata or default to 0
        queue_depth = 0
        try:
            worker_queues_json = redis_client.get(f"worker:{worker_name}:queues")
            if worker_queues_json:
                worker_queues = json.loads(worker_queues_json)
                # Sum up depths across all queues this worker listens to
                for queue_name in worker_queues:
                    queue_depth += get_queue_depth(queue_name)
        except Exception:
            pass  # Queue depth is best-effort

        if not current_task_json:
            # Worker is idle
            return {
                'is_busy': False,
                'current_task': None,
                'estimated_free_in_seconds': 0,
                'queue_depth': queue_depth
            }

        # Worker is busy - parse task info
        current_task = json.loads(current_task_json)

        # Calculate remaining time
        now = time.time()
        estimated_completion = current_task['estimated_completion']
        remaining = max(0, estimated_completion - now)

        return {
            'is_busy': True,
            'current_task': current_task,
            'estimated_free_in_seconds': remaining,
            'queue_depth': queue_depth
        }

    except Exception as e:
        logger.error(f"Failed to get worker availability: {e}")
        # Assume worker is available if we can't determine
        return {
            'is_busy': False,
            'current_task': None,
            'estimated_free_in_seconds': 0,
            'queue_depth': 0
        }


def get_all_workers_availability() -> Dict[str, Dict[str, Any]]:
    """
    Get availability for all workers with current tasks.

    Returns:
        dict: {
            'gpu-server-1': {...availability...},
            'cpu-pool-2': {...availability...}
        }
    """
    try:
        # Find all worker keys using SCAN (non-blocking)
        worker_keys = scan_keys("worker:*:current_task")

        availability = {}
        for key in worker_keys:
            # Extract worker name from key: "worker:<name>:current_task"
            worker_name = key.replace("worker:", "").replace(":current_task", "")
            availability[worker_name] = get_worker_availability(worker_name)

        return availability

    except Exception as e:
        logger.error(f"Failed to get all workers availability: {e}")
        return {}


def predict_queue_time(
    queue_name: str,
    job_estimated_duration: float,
    available_workers: List[Dict[str, Any]]
) -> Dict[str, float]:
    """
    For each worker serving this queue, calculate total time (wait + execution).

    Args:
        queue_name: Queue to route to (e.g., "gpu-vram-high")
        job_estimated_duration: How long this job will take on each worker
        available_workers: List of workers serving this queue with hardware specs

    Returns:
        dict: {
            'gpu-server-1': 305.5,  # seconds until job completes
            'cpu-pool-2': 125.2,
        }
    """
    predictions = {}

    for worker in available_workers:
        worker_name = worker['worker_name']

        # Check if worker serves this queue
        if queue_name not in worker.get('queues', []):
            continue

        # Get worker availability
        availability = get_worker_availability(worker_name)

        # Calculate total time
        wait_time = availability['estimated_free_in_seconds']
        exec_time = job_estimated_duration  # Could be worker-specific via ML
        queue_depth_penalty = availability['queue_depth'] * job_estimated_duration

        total_time = wait_time + exec_time + queue_depth_penalty

        predictions[worker_name] = total_time

    return predictions


def get_task_eta(workflow_run_id: int) -> Optional[Dict[str, Any]]:
    """
    Get ETA information for a running task.

    Args:
        workflow_run_id: Workflow run ID

    Returns:
        dict or None: {
            'started_at': timestamp,
            'estimated_duration': seconds,
            'elapsed': seconds,
            'remaining': seconds,
            'progress_percent': float
        }
    """
    try:
        task_json = redis_client.get(f"task:{workflow_run_id}:eta")

        if not task_json:
            return None

        task = json.loads(task_json)

        now = time.time()
        started_at = task['started_at']
        estimated_duration = task['estimated_duration']

        elapsed = now - started_at
        remaining = max(0, estimated_duration - elapsed)
        progress = min(100, (elapsed / estimated_duration) * 100) if estimated_duration > 0 else 0

        return {
            'workflow_run_id': workflow_run_id,
            'node_id': task.get('node_id'),
            'started_at': started_at,
            'estimated_duration': estimated_duration,
            'elapsed': elapsed,
            'remaining': remaining,
            'progress_percent': progress,
            'metadata': task.get('metadata', {})
        }

    except Exception as e:
        logger.error(f"Failed to get task ETA: {e}")
        return None


def cleanup_stale_tasks(max_age_hours: int = 24):
    """
    Clean up stale task entries (tasks that never completed/failed).

    Should be run periodically (e.g., daily cron job).

    Args:
        max_age_hours: Remove tasks older than this many hours
    """
    try:
        cutoff_time = time.time() - (max_age_hours * 3600)

        # Find all task ETA keys using SCAN (non-blocking)
        task_keys = scan_keys("task:*:eta")

        deleted_count = 0
        for key in task_keys:
            task_json = redis_client.get(key)
            if task_json:
                task = json.loads(task_json)
                if task.get('started_at', 0) < cutoff_time:
                    redis_client.delete(key)
                    deleted_count += 1

        # Find all worker current_task keys using SCAN (non-blocking)
        worker_keys = scan_keys("worker:*:current_task")

        for key in worker_keys:
            task_json = redis_client.get(key)
            if task_json:
                task = json.loads(task_json)
                if task.get('started_at', 0) < cutoff_time:
                    redis_client.delete(key)
                    deleted_count += 1

        logger.info(f"🧹 Cleaned up {deleted_count} stale task entries (older than {max_age_hours}h)")

    except Exception as e:
        logger.error(f"Failed to cleanup stale tasks: {e}")


# Example usage from a worker container:
#
#   from utils.queue_state import register_task_start, register_task_complete
#
#   register_task_start(
#       worker_name=worker_id,
#       workflow_run_id=workflow_run_id,
#       node_id="albedo_process",
#       estimated_duration_seconds=300,
#       metadata={"file_size_mb": 500, "node_kind": "albedo_texture_process"},
#   )
#   try:
#       result = process_albedo_texture()
#   finally:
#       register_task_complete(worker_name=worker_id, workflow_run_id=workflow_run_id)
