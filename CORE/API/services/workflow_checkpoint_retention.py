"""expr 9.6 — daily checkpoint TTL sweep (soft-delete only, default 30d).

ISO 27001 owner rule: MOD NEVER hard-deletes. This sweep only stamps
``deleted_at`` on ``workflow_checkpoints`` rows that have passed their TTL.
There is no DELETE statement anywhere in this module.

Table (models.WorkflowCheckpoint, models.py ~3676; the ``deleted_at`` column
comes from migration 20261012o and is NOT in models.py yet — the lead adds the
ORM column later, so every reference here is raw ``sqlalchemy.text()`` SQL and
never the ORM attribute):

    workflow_checkpoints — id, workflow_run_id, node_run_id, attempt, label,
                           progress, payload JSONB, completed_at, deleted_at

Style precedents: services/workflow_memory_retention.py (expr 9.11 — psr
register + validators, batched ``UPDATE … RETURNING id`` inside
``db.begin_nested()`` per batch, _MAX_BATCHES, SQLAlchemyError → class-name log,
no ``db.rollback()``) and services/workflow_checkpoint_quota.py (expr 9.5 — the
checkpoint table's tenant comes via ``workflow_runs``; the table has no tenant
column of its own).

Run-liveness guard: a resumable long-running run must never lose its latest
checkpoint, so a checkpoint is skipped while its ``workflow_run`` is
non-terminal. ``workflow_runs.status`` is the ``taskstatus`` PG enum and
SQLAlchemy stores the Python enum MEMBER NAMES (UPPERCASE) as the labels, so
the terminal set is listed explicitly here as names.

Per-tenant retention override: the platform-settings registry supports
tenant-scoped rows, but no tenant-level checkpoint-retention override is
registered today — the sweep applies the platform default to all tenants
(same documented limitation as expr 9.11).
"""
from __future__ import annotations

from product_names import themed_logger
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from services import platform_settings as psr

logger = themed_logger(__name__, 'workflows')

_TABLE = "workflow_checkpoints"
_RUNS = "workflow_runs"

# --- Settings registration ----------------------------------------------------

_RETENTION_KEY = "expr.checkpoint_retention_days"
_DEFAULT_RETENTION_DAYS = 30

_BATCH_KEY = "expr.checkpoint_retention_batch_size"
_DEFAULT_BATCH_SIZE = 500

_MAX_BATCHES = 20  # cap per call to keep one scheduler tick bounded


def _validate_positive_int(key_name: str, value, lo: int, hi: int) -> None:
    """Value must be an int in [lo, hi]."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key_name}: must be an int, got {value!r}")
    if value < lo or value > hi:
        raise ValueError(f"{key_name}: must be {lo}..{hi}, got {value!r}")


def _val_retention_days(value) -> None:
    _validate_positive_int("checkpoint_retention_days", value, 1, 3650)


def _val_batch_size(value) -> None:
    _validate_positive_int("checkpoint_retention_batch_size", value, 1, 10000)


psr.register(
    _RETENTION_KEY,
    int,
    _DEFAULT_RETENTION_DAYS,
    validator=_val_retention_days,
    description="expr 9.6 — retention window in days for workflow checkpoints "
                "(completed_at older than N days from now is soft-deleted, "
                "unless its workflow_run is still non-terminal). Default 30, "
                "range 1..3650. No env var.",
    group="expr",
)

psr.register(
    _BATCH_KEY,
    int,
    _DEFAULT_BATCH_SIZE,
    validator=_val_batch_size,
    description="expr 9.6 — batch size for the checkpoint TTL sweep UPDATE "
                "loop. Default 500, range 1..10000.",
    group="expr",
)

# --- Terminal run statuses ----------------------------------------------------

# models.TaskStatus (models.py ~155) member names, which is what SQLAlchemy
# writes into the `taskstatus` PG enum. Migration 20261012l adds SUSPENDED and
# AWAITING_EXTERNAL_EVENT to that enum; 'PAUSED' is not a member today but is
# listed defensively so a future label can never make a paused run look done.
# BLOCKED is deliberately NOT terminal — same posture as
# services/adversary_collect_sweep._TERMINAL_SQL and
# services/autonomous_canary_stats._TERMINAL_STATUSES, which treat only
# COMPLETED/SUCCESS/FAILED/FAILURE as finished.
_TERMINAL_SQL = ("COMPLETED", "SUCCESS", "FAILED", "FAILURE")


def _setting(key: str, default, db):
    """Resolve a setting, fail-closed to the registered default."""
    try:
        return psr.get_setting(key, db=db)
    except Exception:  # noqa: BLE001 — a broken settings layer keeps the default
        logger.warning("workflow_checkpoint_retention: setting unreadable: %s",
                       key)
        return default


def _sanitise_int(value, lo: int, hi: int, default: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(lo, min(value, hi))


# --- Core sweep logic ---------------------------------------------------------


def sweep(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Soft-delete checkpoints past their TTL, skipping live runs.

    A row qualifies when ``completed_at <= now - retention_days``,
    ``deleted_at IS NULL`` and its ``workflow_run`` is NOT non-terminal
    (``workflow_runs`` is joined by name; a checkpoint whose run row is gone
    entirely is swept, since nothing resumable owns it any more).

    Batches run inside their own ``db.begin_nested()`` (SAVEPOINT): on error
    only that SAVEPOINT rolls back, so no ``db.rollback()`` is needed (a full
    rollback would discard work already applied). At most _MAX_BATCHES batches
    per call; if matches exceed ``batch × _MAX_BATCHES`` the remainder waits
    for the next tick.

    Commits ONCE at the end of a successful sweep.

    Never logs payload/label values — only counts and exception class names.

    Returns ``{"soft_deleted": n, "retention_days": d}``, or
    ``{"error": "<ExceptionClass>"}`` if the sweep itself failed.
    """
    now = now or datetime.now(timezone.utc)
    retention_days = _sanitise_int(
        _setting(_RETENTION_KEY, _DEFAULT_RETENTION_DAYS, db), 1, 3650,
        _DEFAULT_RETENTION_DAYS)
    batch_size = _sanitise_int(
        _setting(_BATCH_KEY, _DEFAULT_BATCH_SIZE, db), 1, 10000,
        _DEFAULT_BATCH_SIZE)
    cutoff = now - timedelta(days=retention_days)
    names = ", ".join(f"'{n}'" for n in _TERMINAL_SQL)

    total = 0
    try:
        for _ in range(_MAX_BATCHES):
            with db.begin_nested():
                sql = text(
                    f"""
UPDATE {_TABLE}
SET deleted_at = :now
WHERE id IN (
    SELECT id FROM {_TABLE}
    WHERE completed_at <= :cutoff
      AND deleted_at IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM {_RUNS} AS wr
          WHERE wr.id = workflow_run_id
            AND wr.status NOT IN ({names})
      )
    LIMIT :batch
)
RETURNING id
"""
                )
                result = db.execute(sql, {"now": now, "cutoff": cutoff,
                                          "batch": batch_size})
                deleted = len(result.fetchall())
            if deleted == 0:
                break
            total += deleted
    except SQLAlchemyError as err:
        # The failing `with db.begin_nested()` block already rolled back to
        # its SAVEPOINT on exception exit. Do NOT call db.rollback() here —
        # that is a full-session rollback (precedent: workflow_memory_retention
        # keeps pending work from other tables; here it keeps earlier batches
        # of this table). Only the exception's class name is logged — never a
        # payload/label value.
        cls = type(err).__name__
        logger.exception("checkpoint retention sweep failed: %s", cls)
        return {"error": cls}

    db.commit()
    return {"soft_deleted": total, "retention_days": retention_days}
