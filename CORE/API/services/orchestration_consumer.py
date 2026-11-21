"""Dogfood MOD's own task queue for workflow orchestration.

scale-tiered-workloads D2 (design.md) / tasks §4.

The platform already IS a Postgres-backed queue + worker system (models.ApiTaskQueue,
drained today by the Go worker for containerized build/calibration/benchmark tasks).
Workflow orchestration, however, still runs as an in-process FastAPI
`BackgroundTasks` in the WEB request workers: `run_workflow` calls
`background_tasks.add_task(start_workflow_execution, ...)`, which does all the
per-file DAG planning/enqueue INLINE, in the same process that serves HTTP. Under a
big fan-out that pins 6-9 web CPUs and Execute/UI polls go sluggish.

This module moves that work off the request path WITHOUT building a bespoke
background service — it dogfoods the existing queue:

  * Execute (`run_workflow`) enqueues ONE `orchestration` ApiTaskQueue row carrying
    {workflow_run_id, full_conf} and returns 202 immediately (enqueue_orchestration_task).
  * The `background` role runs a consumer loop that claims those rows with the same
    FOR UPDATE SKIP LOCKED atomic-claim the Go worker uses, and runs
    start_workflow_execution IN-PROCESS (it needs the DB + app context; we do NOT
    spawn a container per orchestration — this queue_name is drained by the API
    background role, not the Go worker).

Everything here is behind MOD_ORCHESTRATION_VIA_QUEUE, which DEFAULTS FALSE. With the
flag off, `run_workflow` keeps its exact in-process BackgroundTasks path and this
consumer is never started, so a default deploy behaves precisely as it does today.

D2 note on replicas: the consumer may run on ALL background replicas — the row-claim
(SKIP LOCKED) makes it naturally exclusive per row, so it needs NO leader lock (unlike
the singleton scheduler sweeps, which keep theirs).

GC.STW.6/7 — NOTIFY wake: `enqueue_orchestration_task` fires
`pg_notify('mod_orchestration', id)` in the SAME transaction as the row INSERT (so
Postgres only delivers it once that transaction commits), and the consumer LISTENs
on that channel over a dedicated, non-PgBouncer connection to wake instantly instead
of waiting for the next poll tick. The 0.5s poll is kept as a permanent fallback: it
still drains the queue on its own if NOTIFY is unavailable (no direct/non-pooled DB
connection to LISTEN on) or if a notify is ever missed (reconnect window, etc.).
"""

import logging
import os
import select
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# The internal queue this consumer owns. Distinct from the CPU/CUDA/ROCm/Intel
# queues the Go worker drains, so the Go worker never picks these up.
ORCHESTRATION_QUEUE_NAME = "orchestration"

# ApiTaskQueue.node_id is NOT NULL and is meant for a real graph node id; an
# orchestration task is not tied to a single node, so use an explicit sentinel
# rather than a misleading real-looking value.
ORCHESTRATION_NODE_ID = "__orchestration__"

# pg_notify channel the enqueue path fires and the consumer LISTENs on (GC.STW.6/7).
ORCHESTRATION_NOTIFY_CHANNEL = "mod_orchestration"

# Module-global consumer thread handle so start_orchestration_consumer() is
# idempotent (the lifespan can, in principle, be re-entered under tests).
_consumer_thread: Optional[threading.Thread] = None
_consumer_stop = threading.Event()

# NOTIFY-wake plumbing: a dedicated listener thread sets this Event when a
# `mod_orchestration` notification arrives; consumer_loop waits on it (bounded by
# the poll interval) instead of sleeping blindly. Shared globally so the default
# start_orchestration_consumer() wiring needs no extra plumbing; tests that want
# isolation drive consumer_loop/_notify_listen_loop directly with their own Event.
_notify_thread: Optional[threading.Thread] = None
_notify_wake = threading.Event()
_notify_warned_no_direct_conn = False


def orchestration_via_queue_enabled() -> bool:
    """Whether Execute routes orchestration through MOD's queue (D2).

    DEFAULT FALSE: unset/empty keeps today's in-process BackgroundTasks path, so
    deploying this code with no env changes is a no-op.
    """
    from services import platform_settings as _psr
    return bool(_psr.get_setting("orchestration_via_queue_enabled"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_orchestration_task(db, *, tenant_id: int, workflow_run_id: int,
                               full_conf: dict, priority: int = 0) -> int:
    """Write one `orchestration` ApiTaskQueue row for a created run and commit it.

    Called from `run_workflow` on the WEB tier when the flag is on. Deliberately
    trivial — a single INSERT + COMMIT — so Execute's cost does not scale with the
    workflow's fan-out; the actual planning happens later, on the background role.

    The row uses only EXISTING ApiTaskQueue columns (no schema/migration): the
    payload carries what start_workflow_execution needs (run id + full_conf).

    GC.STW.6: also issues `pg_notify(ORCHESTRATION_NOTIFY_CHANNEL, <id>)` on the
    SAME `db` session/transaction as the INSERT, via `db.flush()` (to obtain the
    row id) followed by `db.execute(text("SELECT pg_notify(...)"))` BEFORE the
    single `db.commit()` below. Postgres only actually delivers a NOTIFY to
    LISTENers once the issuing transaction commits, so pairing it with the insert
    this way guarantees the wake never fires for a row that failed to commit (and
    never gets lost between the insert and a would-be separate notify call).
    Gated on `orchestration_via_queue_enabled()` for defense in depth — this
    function is only ever called from that flag's branch today, but a future
    caller must not get a silent NOTIFY with the flag off.
    """
    from sqlalchemy import text

    from models import ApiTaskQueue, ApiTaskStatus

    task = ApiTaskQueue(
        workflow_run_id=workflow_run_id,
        node_id=ORCHESTRATION_NODE_ID,
        tenant_id=tenant_id,
        queue_name=ORCHESTRATION_QUEUE_NAME,
        priority=priority,
        task_type="processing",
        task_payload={
            "workflow_run_id": workflow_run_id,
            "full_conf": full_conf,
        },
        status=ApiTaskStatus.PENDING,
    )
    db.add(task)
    db.flush()  # assign task.id without committing yet
    if orchestration_via_queue_enabled():
        db.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": ORCHESTRATION_NOTIFY_CHANNEL, "payload": str(task.id)},
        )
    db.commit()
    # FX.165 p1 (WAKE.0 cause #2): the row committed PENDING with no wake, so
    # drainers only found it on their next poll tick. Publish AFTER the commit
    # (never inside the open transaction), on the row's ACTUAL queue_name, via
    # the same lazy-imported helper every other enqueue site uses
    # (routers/worker_tasks._publish_event). Best-effort: a dead Redis must
    # never fail the enqueue.
    try:
        from routers.worker_tasks import _publish_event

        _publish_event(f"task:available:{task.queue_name}")
    except Exception:  # noqa: BLE001 — wake-up must never fail the enqueue
        logger.debug("orchestration wake publish on %s failed", task.queue_name)
    logger.info("orchestration task %s enqueued for run %s (tenant=%s)",
                task.id, workflow_run_id, tenant_id)
    return task.id


def claim_orchestration_task(db) -> Optional[Tuple[int, dict]]:
    """Atomically claim the next PENDING orchestration row; return (id, payload).

    Mirrors the platform's existing queue-drain claim (FOR UPDATE SKIP LOCKED, as
    in services/scheduler.py's capacity dispatch and routers/worker_tasks.py) so
    two background replicas never grab the same row. Marks the row RUNNING and
    commits BEFORE returning, so the claim is durable and no transaction stays
    open while the (long) orchestration runs — that would pin a PgBouncer backend
    for the whole run (the platform's external-work-in-transaction failure mode).

    Returns None when the queue is empty. The (id, payload) tuple is captured
    while the session is live so the caller can close the session before running.
    """
    from models import ApiTaskQueue, ApiTaskStatus

    task = (
        db.query(ApiTaskQueue)
        .filter(
            ApiTaskQueue.status == ApiTaskStatus.PENDING,
            ApiTaskQueue.queue_name == ORCHESTRATION_QUEUE_NAME,
        )
        .order_by(ApiTaskQueue.priority.desc(), ApiTaskQueue.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
        .first()
    )
    if task is None:
        return None

    task_id = task.id
    payload = dict(task.task_payload or {})
    task.status = ApiTaskStatus.RUNNING
    task.claimed_at = _now()
    task.started_at = _now()
    db.commit()
    logger.info("orchestration task %s claimed (run=%s)", task_id,
                payload.get("workflow_run_id"))
    return task_id, payload


def mark_task_terminal(db, task_id: int, ok: bool,
                       error_message: Optional[str] = None) -> None:
    """Flip a claimed orchestration row to COMPLETED / FAILED after the run.

    Uses a SHORT, separate transaction (the run itself already finished on its
    own sessions), so the terminal write never overlaps the orchestration work.
    """
    from models import ApiTaskQueue, ApiTaskStatus

    task = db.query(ApiTaskQueue).filter(ApiTaskQueue.id == task_id).first()
    if task is None:
        logger.warning("orchestration task %s vanished before terminal update", task_id)
        return
    task.status = ApiTaskStatus.COMPLETED if ok else ApiTaskStatus.FAILED
    task.completed_at = _now()
    if not ok and error_message:
        task.error_message = error_message[:2000]
    db.commit()


def _default_executor(db_factory, full_conf: dict, workflow_run_id: int) -> None:
    """Lazy import so this module imports cleanly without the orchestrator graph."""
    from services.workflow_orchestrator import start_workflow_execution
    start_workflow_execution(db_factory, full_conf, workflow_run_id)


#: GT.C2 — the "dispatch paused" hold is logged at most this often (per
#: process) so a paused consumer loop polling every 0.5s doesn't spam the log.
_drain_log_throttle_s = float(os.getenv("MOD_DRAIN_LOG_THROTTLE_S", "60"))
_last_drain_log_at = 0.0


def _dispatch_paused(db_factory) -> bool:
    """Cheap drain check (cached platform-settings getter). Never raises —
    a settings read failure means "not paused" (dispatch on)."""
    session = db_factory()
    try:
        from services import platform_settings as _psr
        return bool(_psr.get_setting("dispatch.paused", db=session))
    except Exception:  # noqa: BLE001 — degrade to "dispatch on"
        return False
    finally:
        session.close()


def _note_drain_hold() -> None:
    """Log the paused hold at most once per minute (per process)."""
    global _last_drain_log_at
    now = time.monotonic()
    if now - _last_drain_log_at >= _drain_log_throttle_s:
        _last_drain_log_at = now
        logger.info("orchestration consumer: dispatch paused (drain mode) — "
                    "holding queued runs (not failing them); retrying after "
                    "resume")


def consume_once(db_factory, executor: Optional[Callable] = None) -> bool:
    """Claim and run at most one orchestration task. Returns True if it ran one.

    GT.C2 — when dispatch is paused (drain mode) this returns False WITHOUT
    claiming: the pending orchestration row stays PENDING (held, not failed),
    so a run created while paused is picked up as soon as dispatch resumes.
    The hold is logged at most once per minute.

    Sequenced deliberately so no DB transaction is held open during the run:
      1. open a session, claim (commit), close it;
      2. run start_workflow_execution (which manages its OWN sessions);
      3. open a fresh session, write the terminal status, close it.
    """
    executor = executor or _default_executor

    if _dispatch_paused(db_factory):
        _note_drain_hold()
        return False

    session = db_factory()
    try:
        claimed = claim_orchestration_task(session)
    finally:
        session.close()

    if claimed is None:
        return False

    task_id, payload = claimed
    run_id = payload.get("workflow_run_id")
    full_conf = payload.get("full_conf") or {}

    ok = True
    err: Optional[str] = None
    try:
        executor(db_factory, full_conf, run_id)
    except Exception as e:  # noqa: BLE001 — record and continue draining
        ok = False
        err = str(e)
        logger.exception("orchestration task %s (run=%s) failed", task_id, run_id)

    session = db_factory()
    try:
        mark_task_terminal(session, task_id, ok, err)
    except Exception:
        logger.exception("orchestration task %s terminal update failed", task_id)
    finally:
        session.close()
    return True


def consumer_loop(db_factory, stop_event: threading.Event,
                  poll_interval: Optional[float] = None, *,
                  executor: Optional[Callable] = None,
                  wake_event: Optional[threading.Event] = None) -> None:
    """Drain the orchestration queue until stopped.

    Tight poll: drain everything available, then wait for either a NOTIFY wake
    (GC.STW.7 — `wake_event`, set by `_notify_listen_loop` when a
    `mod_orchestration` notification arrives) or the poll interval, whichever
    comes first, when the queue is empty. The poll interval is a permanent
    fallback, not just an initial implementation: it keeps draining even if the
    NOTIFY listener never attaches (e.g. only a PgBouncer-pooled DSN is
    available — see `_resolve_listen_engine`) or a notify is ever missed.

    `executor` is forwarded to `consume_once` (tests substitute a no-op to avoid
    running a real orchestration graph against a test DB).
    """
    if poll_interval is None:
        poll_interval = float(os.getenv("MOD_ORCHESTRATION_POLL_INTERVAL_S", "0.5"))
    if wake_event is None:
        wake_event = _notify_wake
    logger.info("orchestration consumer loop started (poll=%.2fs)", poll_interval)
    while not stop_event.is_set():
        try:
            did_work = consume_once(db_factory, executor=executor)
        except Exception:
            # A claim/DB error must not kill the loop; back off and retry.
            logger.exception("orchestration consumer iteration failed")
            did_work = False
        if not did_work:
            # Bounded by poll_interval regardless of whether NOTIFY ever wakes
            # us, so shutdown (stop_orchestration_consumer also sets wake_event)
            # and the fallback poll both stay responsive.
            wake_event.wait(poll_interval)
            wake_event.clear()
    logger.info("orchestration consumer loop stopped")


def _resolve_listen_engine():
    """Return a genuine direct-to-Postgres SQLAlchemy engine to LISTEN on, or
    None when only a PgBouncer-pooled DSN is available.

    `database.get_direct_engine()` normally bypasses PgBouncer by opening a
    connection straight to `POSTGRES_HOST` with the current Vault credentials
    (see database.py). It falls back to the shared pooled engine
    (`database.get_engine()`, which goes through PgBouncer transaction pooling
    via `POSTGRES_POOLER_HOST` when that differs from `POSTGRES_HOST`) ONLY in
    static/non-Vault mode, where there are no separate credentials to build a
    true direct connection with. LISTEN over a transaction-pooled PgBouncer
    connection silently loses its subscription the moment the connection is
    returned to the pool between statements, so if that fallback happened AND a
    distinct pooler host is actually configured, we must not LISTEN on it at
    all — the caller stays on the 0.5s poll instead.
    """
    import database

    try:
        direct_engine = database.get_direct_engine()
    except Exception:
        logger.warning(
            "orchestration consumer: could not resolve a direct DB engine for "
            "LISTEN; staying on the poll fallback", exc_info=True)
        return None

    try:
        pooled_engine = database.get_engine()
    except Exception:
        pooled_engine = None

    pooler_host = os.getenv("POSTGRES_POOLER_HOST") or os.getenv("POSTGRES_HOST", "")
    direct_host = os.getenv("POSTGRES_HOST", "")
    only_pgbouncer_dsn = (
        direct_engine is not None
        and direct_engine is pooled_engine
        and pooler_host
        and pooler_host != direct_host
    )
    if only_pgbouncer_dsn:
        return None
    return direct_engine


def _default_listen_connection():
    """Build the dedicated raw psycopg2 connection `_notify_listen_loop` LISTENs
    on. Returns None (poll-only fallback) when no safe direct connection exists.
    """
    engine = _resolve_listen_engine()
    if engine is None:
        return None
    raw = engine.raw_connection()
    return getattr(raw, "driver_connection", None) or getattr(raw, "connection", None) or raw


def _notify_listen_loop(stop_event: threading.Event, wake_event: threading.Event,
                        connection_factory: Optional[Callable] = None) -> None:
    """LISTEN on ORCHESTRATION_NOTIFY_CHANNEL over a dedicated raw psycopg2
    connection; set `wake_event` whenever a notification arrives so
    `consumer_loop` wakes immediately instead of waiting out the poll interval.

    Never raises: a connection that cannot be built at all (no safe direct/
    non-PgBouncer route — see `_resolve_listen_engine`) logs ONE warning and
    returns, leaving the poll as the sole (already-safe) drain mechanism. A
    connection that is later lost (network blip, Postgres restart, credential
    rotation) is reconnected with capped exponential backoff; the outer
    consumer_loop keeps polling throughout regardless.
    """
    global _notify_warned_no_direct_conn
    connection_factory = connection_factory or _default_listen_connection
    backoff = 1.0
    while not stop_event.is_set():
        try:
            conn = connection_factory()
        except Exception:
            conn = None
        if conn is None:
            if not _notify_warned_no_direct_conn:
                logger.warning(
                    "orchestration consumer: no direct (non-PgBouncer) DB "
                    "connection available for LISTEN/NOTIFY — staying on the "
                    "%.2fs poll fallback",
                    float(os.getenv("MOD_ORCHESTRATION_POLL_INTERVAL_S", "0.5")))
                _notify_warned_no_direct_conn = True
            return
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(f"LISTEN {ORCHESTRATION_NOTIFY_CHANNEL};")
            logger.info("orchestration consumer: LISTEN attached to %s",
                        ORCHESTRATION_NOTIFY_CHANNEL)
            backoff = 1.0  # reset after a successful attach
            while not stop_event.is_set():
                if select.select([conn], [], [], 5) == ([], [], []):
                    continue
                conn.poll()
                while conn.notifies:
                    conn.notifies.pop(0)
                    wake_event.set()
        except Exception as e:  # noqa: BLE001 — reconnect after a backoff
            logger.warning(
                "orchestration consumer: LISTEN connection lost (%s) — "
                "reconnecting in %.1fs", e, backoff)
            stop_event.wait(backoff)
            backoff = min(backoff * 2, 30.0)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    logger.info("orchestration consumer NOTIFY listener stopped")


def start_orchestration_consumer(db_factory=None, *,
                                 listen_connection_factory: Optional[Callable] = None
                                 ) -> Optional[threading.Thread]:
    """Spawn the consumer as a daemon thread, plus a second daemon thread that
    LISTENs for the NOTIFY wake (GC.STW.7). Idempotent; started by the
    background role (main.py lifespan) only when the flag is on."""
    global _consumer_thread, _notify_thread
    if _consumer_thread is not None and _consumer_thread.is_alive():
        return _consumer_thread

    if db_factory is None:
        from database import SessionLocal
        db_factory = SessionLocal

    _consumer_stop.clear()
    _notify_wake.clear()
    _consumer_thread = threading.Thread(
        target=consumer_loop,
        args=(db_factory, _consumer_stop),
        kwargs={"wake_event": _notify_wake},
        name="OrchestrationConsumer",
        daemon=True,
    )
    _consumer_thread.start()

    if _notify_thread is None or not _notify_thread.is_alive():
        _notify_thread = threading.Thread(
            target=_notify_listen_loop,
            args=(_consumer_stop, _notify_wake, listen_connection_factory),
            name="OrchestrationNotifyListener",
            daemon=True,
        )
        _notify_thread.start()
    return _consumer_thread


def stop_orchestration_consumer() -> None:
    """Signal the consumer loop AND the NOTIFY listener to exit (best-effort;
    used on shutdown/tests). Setting `_notify_wake` too means a blocked
    `consumer_loop` wait wakes immediately rather than waiting out the poll
    interval before it notices `_consumer_stop`."""
    _consumer_stop.set()
    _notify_wake.set()
