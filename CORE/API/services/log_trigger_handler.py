"""Log-pattern trigger handler.

A `logging.Handler` registered on the root logger at API startup. On every
record at WARNING+ it walks an in-memory snapshot of active
`log_pattern_alert` triggers, evaluates the regex (cooldown-first to skip
the regex match cheaply when in cooldown), and enqueues firings onto an
`asyncio.Queue`. A startup-registered consumer task drains the queue and
fires runs in fresh DB sessions — keeping log-emit threads off the
orchestrator critical path.

The snapshot is rebuilt at startup and on every `log_pattern_alert` CRUD
write (router calls `refresh_active_snapshot(db)` after the commit).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')


# ---------------------------------------------------------------------------
# Snapshot entry
# ---------------------------------------------------------------------------


@dataclass
class _SnapshotEntry:
    trigger_id: int
    workflow_id: int
    tenant_id: int
    pattern: re.Pattern
    log_level: str  # "WARNING" / "ERROR" / "CRITICAL" / "any"
    match_field: str  # "message" / "module" / "any"
    cooldown_seconds: int
    created_by_user_id: Optional[int]
    last_fired_at: Optional[datetime] = field(default=None)


_LEVEL_TO_INT = {
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "any": logging.WARNING,  # treat "any" as ≥WARNING (handler floor)
}


# ---------------------------------------------------------------------------
# Singleton handler
# ---------------------------------------------------------------------------


class LogPatternTriggerHandler(logging.Handler):
    """Emits firings to an asyncio.Queue when a record matches an active
    log_pattern_alert trigger.

    Recommended registration: `logger.getLogger().addHandler(handler)` with
    `handler.setLevel(logging.WARNING)` so DEBUG/INFO records short-circuit
    before reaching `emit`.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._snapshot: dict[int, _SnapshotEntry] = {}
        self._lock = threading.Lock()
        self._queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- lifecycle ----------------------------------------------------------

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Wire the handler to an asyncio loop. Must be called before the
        consumer task starts. Safe to call multiple times — the most recent
        loop wins.
        """
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=10_000)

    def get_queue(self) -> Optional[asyncio.Queue]:
        return self._queue

    # -- snapshot management -----------------------------------------------

    def replace_snapshot(self, entries: list[_SnapshotEntry]) -> None:
        with self._lock:
            self._snapshot = {e.trigger_id: e for e in entries}

    def snapshot_size(self) -> int:
        with self._lock:
            return len(self._snapshot)

    # -- emit ---------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        # WARNING-floor short-circuit. handler.level is set in __init__ but
        # check defensively in case a caller lowered it.
        if record.levelno < logging.WARNING:
            return

        # Snapshot-empty short-circuit — most records hit this path.
        with self._lock:
            if not self._snapshot:
                return
            entries = list(self._snapshot.values())

        if not entries:
            return

        try:
            message = record.getMessage()
        except Exception:
            message = str(getattr(record, "msg", ""))
        module = getattr(record, "module", "")

        now = datetime.now(timezone.utc)

        for entry in entries:
            # 1. Cooldown FIRST — cheaper than regex matching.
            if entry.last_fired_at is not None:
                elapsed = (now - entry.last_fired_at).total_seconds()
                if elapsed < entry.cooldown_seconds:
                    continue

            # 2. Level filter.
            if entry.log_level != "any":
                threshold = _LEVEL_TO_INT.get(entry.log_level, logging.WARNING)
                if record.levelno < threshold:
                    continue

            # 3. Regex match against the configured field.
            if entry.match_field == "module":
                target = module
            elif entry.match_field == "any":
                target = f"{module} {message}"
            else:
                target = message

            try:
                if not entry.pattern.search(target):
                    continue
            except Exception:
                continue

            # 4. Update last_fired_at + enqueue firing.
            entry.last_fired_at = now
            self._enqueue_firing(entry, target[:500])

    def _enqueue_firing(self, entry: _SnapshotEntry, summary: str) -> None:
        if self._queue is None or self._loop is None:
            logger.debug(
                "log_trigger_handler: no asyncio loop attached — dropping firing for trigger %d",
                entry.trigger_id,
            )
            return

        payload = {
            "trigger_id": entry.trigger_id,
            "workflow_id": entry.workflow_id,
            "tenant_id": entry.tenant_id,
            "created_by_user_id": entry.created_by_user_id,
            "summary": summary,
        }

        # We're potentially called from a non-async thread (the request
        # thread or a worker). asyncio.Queue.put_nowait is not thread-safe;
        # use call_soon_threadsafe to enqueue from the loop's thread.
        try:
            self._loop.call_soon_threadsafe(self._safe_put_nowait, payload)
        except RuntimeError:
            # Loop closed — handler is going away. Drop silently.
            pass

    def _safe_put_nowait(self, payload: dict) -> None:
        try:
            assert self._queue is not None
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning(
                "log_trigger_handler: queue full — dropping firing for trigger %s",
                payload.get("trigger_id"),
            )


# ---------------------------------------------------------------------------
# Module-level singleton + helpers
# ---------------------------------------------------------------------------

_handler_singleton: Optional[LogPatternTriggerHandler] = None
_handler_lock = threading.Lock()


def get_handler() -> LogPatternTriggerHandler:
    """Return the process-wide handler singleton. Lazily constructed."""
    global _handler_singleton
    with _handler_lock:
        if _handler_singleton is None:
            _handler_singleton = LogPatternTriggerHandler()
        return _handler_singleton


def refresh_active_snapshot(db) -> None:
    """Re-load active log_pattern_alert triggers from the DB into the
    handler's in-memory snapshot. Called from API startup AND from the
    workflow_triggers router after a successful CRUD write.

    Tolerant of being called before the handler is attached to a loop —
    the snapshot is updated regardless, so it's ready as soon as the
    consumer task starts.
    """
    try:
        from models import WorkflowTrigger, WorkflowTriggerType
    except Exception:
        return

    handler = get_handler()
    try:
        rows = (
            db.query(WorkflowTrigger)
            .filter(
                WorkflowTrigger.trigger_type == WorkflowTriggerType.LOG_PATTERN_ALERT,
                WorkflowTrigger.is_active.is_(True),
                WorkflowTrigger.deleted_at.is_(None),
            )
            .all()
        )
    except Exception as e:
        logger.warning("refresh_active_snapshot DB query failed: %s", e)
        return

    entries: list[_SnapshotEntry] = []
    for row in rows:
        cfg = row.configuration or {}
        pattern_str = cfg.get("pattern")
        if not pattern_str:
            continue
        try:
            compiled = re.compile(pattern_str)
        except re.error:
            logger.warning(
                "log_pattern_alert trigger %s has invalid regex %r — skipping",
                row.id, pattern_str,
            )
            continue
        entries.append(_SnapshotEntry(
            trigger_id=row.id,
            workflow_id=row.workflow_id,
            tenant_id=row.tenant_id,
            pattern=compiled,
            log_level=cfg.get("log_level", "ERROR"),
            match_field=cfg.get("match_field", "message"),
            cooldown_seconds=int(cfg.get("cooldown_seconds", 60)),
            created_by_user_id=row.created_by_user_id,
            last_fired_at=row.last_triggered_at,
        ))

    handler.replace_snapshot(entries)
    logger.info(
        "log_trigger_handler: snapshot refreshed (%d active log_pattern_alert triggers)",
        len(entries),
    )


# ---------------------------------------------------------------------------
# Consumer task
# ---------------------------------------------------------------------------


async def log_trigger_consumer() -> None:
    """Drain the handler's asyncio queue. Each entry triggers one
    `dispatch_trigger_run` call against a fresh DB session, wrapped in
    try/except so a single firing failure can't kill the consumer.

    Started from main.py's startup hook after the asyncio loop is up.
    """
    handler = get_handler()
    queue = handler.get_queue()
    if queue is None:
        logger.warning("log_trigger_consumer: no queue attached — exiting")
        return

    from database import SessionLocal
    from services.workflow_orchestrator import dispatch_trigger_run
    from models import Workflow, WorkflowTrigger
    from datetime import datetime, timezone

    while True:
        try:
            payload = await queue.get()
        except asyncio.CancelledError:
            break

        db = SessionLocal()
        try:
            workflow = (
                db.query(Workflow)
                .filter(
                    Workflow.id == payload["workflow_id"],
                    Workflow.tenant_id == payload["tenant_id"],
                )
                .first()
            )
            if not workflow:
                continue

            dispatch_trigger_run(
                db,
                workflow=workflow,
                triggered_by_user_id=payload.get("created_by_user_id"),
                triggered_by_trigger_id=payload["trigger_id"],
                extra_inputs={"log_record_summary": payload.get("summary", "")},
                chain_depth=0,
            )

            trigger = (
                db.query(WorkflowTrigger)
                .filter(WorkflowTrigger.id == payload["trigger_id"])
                .first()
            )
            if trigger:
                trigger.last_triggered_at = datetime.now(timezone.utc)
                trigger.trigger_count = (trigger.trigger_count or 0) + 1
                db.commit()

        except Exception as e:
            logger.exception(
                "log_trigger_consumer: firing trigger %s failed: %s",
                payload.get("trigger_id"), e,
            )
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()
