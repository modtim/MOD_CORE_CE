"""expr 9.10 (slice) — soft-delete + restore of a workflow's memory rows.

``workflow_memory_kv`` / ``workflow_memory_vector`` /
``workflow_memory_timeseries`` (expr 1.8,
``migrations/20261012h_workflow_memory_tables.py``) are keyed by
``(tenant_id, workflow_slug)`` — NOT ``workflow_id`` (migration docstring
~16-23, design Decision 11 / slug-scoping): the slug-like column on the
workflows table is ``workflows.name`` (models.py:2132), which is exactly what
``services/sub_workflow_validation.py:110`` already matches a
``workflow_slug`` ref against.  So deleting a workflow tombstones the memory of
its CURRENT name; renaming/re-versioning never moves memory.

These tables are NOT in models.py, so this module speaks plain ``text()`` SQL
only — no ORM model, no models.py edit.

ISO 27001: MOD never hard-deletes.  There is no ``DELETE`` statement anywhere
in this module: deletion stamps ``deleted_at``, restore clears it back to NULL.

Restore is EXACT-TIMESTAMP scoped (``deleted_at = :d``): rows swept by the 9.11
retention sweep, or individually tombstoned earlier by
``services/workflow_memory_kv.delete_kv``, carry a different ``deleted_at`` and
therefore stay deleted.  The window is
``workflow_memory.recovery_window_days`` (int, default 30, 1..365, tenant-
overridable) — registered here, resolved through
``services/platform_settings`` with the same fail-closed shape as
``services/adversary_rate_limit.py``.

Transaction discipline: each table gets its OWN ``db.begin_nested()`` savepoint,
so a broken table (missing/not-yet-migrated, FK or CHECK violation) poisons
only that table's savepoint — the other two still get processed and the caller's
session stays usable.  This module NEVER commits (the caller owns the commit)
and NEVER raises: a failed table yields ``None`` in the per-table counts and
logs the exception CLASS NAME only — never values, never key text, never the
DSN.
"""
from __future__ import annotations

from product_names import themed_logger
from typing import Optional
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from services import platform_settings as psr
from services.workflow_memory_kv import (
    MAX_SLUG_CHARS,
    _validate_slug,
    _TABLE as _KV,
)

logger = themed_logger(__name__, 'workflows')

_VECTOR = "workflow_memory_vector"
_SERIES = "workflow_memory_timeseries"

# The three memory stores, in the order they are processed.
_TABLES = (_KV, _VECTOR, _SERIES)

RECOVERY_WINDOW_KEY = "workflow_memory.recovery_window_days"
DEFAULT_RECOVERY_WINDOW_DAYS = 30


def _validate_recovery_window_days(value) -> None:
    """Honest-range rule for the recovery window: 1..365 days.

    Zero would mean "restore is impossible", which is what a flag is for;
    a year is the longest window the design asks for (30-day default).
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < 1 or value > 365:
        raise ValueError(f"must be 1..365, got {value!r}")


psr.register(
    RECOVERY_WINDOW_KEY,
    int,
    DEFAULT_RECOVERY_WINDOW_DAYS,
    validator=_validate_recovery_window_days,
    description="expr 9.10 (tasks.md 9.10) — how long a workflow's soft-deleted "
                "memory rows stay recoverable after the workflow-deletion "
                "timestamp stamps them. Past this window restore_workflow_memory "
                "refuses with window_expired. Default 30 (design.md: 30-day "
                "recovery window); a tenant-scope row can raise/lower it. "
                "Min 1 — turning recovery off is a flag's job, not a "
                "window's.",
    group="workflow_memory",
)


def _recovery_window_days(db: Optional[Session], tenant_id: Optional[int]) -> int:
    """Resolve the recovery window, fail-closed to the registered default."""
    try:
        value = psr.get_setting(RECOVERY_WINDOW_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — fail-closed: a broken settings layer
        logger.exception("workflow-memory recovery window unreadable; using default")
        return DEFAULT_RECOVERY_WINDOW_DAYS
    try:
        _validate_recovery_window_days(value)
    except ValueError:
        logger.warning("workflow-memory recovery window out of range: %r", value)
        return DEFAULT_RECOVERY_WINDOW_DAYS
    return int(value)


def _workflow_slug(db: Session, tenant_id: int, workflow_id: int) -> Optional[str]:
    """The memory slug (== ``workflows.name``) for one workflow, or None.

    Tenant-scoped, and SOFT-DELETED workflows count: this runs during / just
    after the workflow deletion, when the row already carries ``deleted_at``.
    Plain ``text()`` SQL — the memory tables are not in models.py and neither
    is this lookup needed as an ORM row.
    """
    row = db.execute(
        text("SELECT name FROM workflows "
             "WHERE id = :w AND tenant_id = :t"),
        {"w": int(workflow_id), "t": int(tenant_id)},
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return _validate_slug(row[0])
    except ValueError:
        logger.warning("workflow-memory slug unusable for workflow_id=%r",
                       workflow_id)
        return None


def soft_delete_workflow_memory(db: Session, *, tenant_id: int,
                                workflow_id: int,
                                now: Optional[datetime] = None) -> dict:
    """SOFT-delete every live memory row of one workflow, across all 3 tables.

    Stamps ``deleted_at = :now`` on rows matching ``(tenant_id, workflow_slug)``
    that are still live (``deleted_at IS NULL``) — rows already tombstoned
    (individually deleted, or retention-swept earlier) keep their ORIGINAL
    ``deleted_at`` and are not re-stamped, which is what makes
    :func:`restore_workflow_memory` exact-timestamp scoped.

    Returns ``{"status", "workflow_slug", "deleted_at", "tables": {...}}`` where
    ``tables`` maps table name -> rowcount (``None`` when that table's update
    failed).  Never commits, never raises, never hard-deletes.
    """
    now = now or datetime.now(timezone.utc)
    slug = _workflow_slug(db, tenant_id, workflow_id)
    if slug is None:
        return {"status": "workflow_not_found", "workflow_slug": None,
                "deleted_at": None, "tables": {}}

    counts: dict[str, Optional[int]] = {}
    for table in _TABLES:
        try:
            with db.begin_nested():
                counts[table] = db.execute(
                    text(f"UPDATE {table} SET deleted_at = :d "
                         "WHERE tenant_id = :t AND workflow_slug = :s "
                         "AND deleted_at IS NULL"),
                    {"d": now, "t": int(tenant_id), "s": slug},
                ).rowcount
        except SQLAlchemyError as e:
            counts[table] = None
            logger.warning("workflow-memory soft delete failed on %s: %s",
                           type(e).__name__, e)

    return {"status": "ok", "workflow_slug": slug,
            "deleted_at": now, "tables": counts}


def restore_workflow_memory(db: Session, *, tenant_id: int,
                            workflow_id: int, deleted_at: datetime,
                            recovery_days: Optional[int] = None) -> dict:
    """Undo ONE soft deletion: clears ``deleted_at`` on rows stamped exactly ``deleted_at``.

    Exact-timestamp match (``deleted_at = :d``) — rows tombstoned by the 9.11
    retention sweep, or individually deleted earlier by
    ``workflow_memory_kv.delete_kv``, carry a different timestamp and stay
    deleted.  Refuses with ``{"status": "window_expired"}`` once
    ``now - deleted_at`` exceeds the recovery window (setting
    ``workflow_memory.recovery_window_days``, tenant-overridable; an explicit
    ``recovery_days`` overrides it for this call only).

    Same transaction discipline as :func:`soft_delete_workflow_memory`: one
    savepoint per table, failed table -> ``None``, never commits, never raises.
    """
    now = datetime.now(timezone.utc)
    if isinstance(deleted_at, str):
        try:
            deleted_at = datetime.fromisoformat(deleted_at)
        except ValueError:
            return {"status": "bad_timestamp", "workflow_slug": None,
                    "deleted_at": None, "tables": {}}

    window = (recovery_days if recovery_days is not None
              else _recovery_window_days(db, tenant_id))
    if (now - deleted_at) > timedelta(days=window):
        return {"status": "window_expired", "workflow_slug": None,
                "deleted_at": deleted_at, "tables": {}}

    slug = _workflow_slug(db, tenant_id, workflow_id)
    if slug is None:
        return {"status": "workflow_not_found", "workflow_slug": None,
                "deleted_at": deleted_at, "tables": {}}

    counts: dict[str, Optional[int]] = {}
    for table in _TABLES:
        try:
            with db.begin_nested():
                counts[table] = db.execute(
                    text(f"UPDATE {table} SET deleted_at = NULL "
                         "WHERE tenant_id = :t AND workflow_slug = :s "
                         "AND deleted_at = :d"),
                    {"t": int(tenant_id), "s": slug, "d": deleted_at},
                ).rowcount
        except SQLAlchemyError as e:
            counts[table] = None
            logger.warning("workflow-memory restore failed on %s: %s",
                           type(e).__name__, e)

    return {"status": "ok", "workflow_slug": slug,
            "deleted_at": deleted_at, "tables": counts}
