"""expr 9.11 — daily memory-retention sweep (soft-delete only).

ISO 27001 owner rule: MOD never hard-deletes — this sweep only stamps
``deleted_at`` on rows that have passed their TTL.

Tables (migration 20261012h, NOT in models.py — text() SQL only):

    workflow_memory_kv       – expires_at, deleted_at
    workflow_memory_timeseries – ts, deleted_at
    workflow_memory_vector     – created_at, deleted_at (no TTL in spec → untouched)

Style precedent for settings: services/adversary_rate_limit.py (psr.register
+ validator, tenant-overridable). Sibling services for logging/savepoint style:
services/workflow_memory_timeseries.py.

Per-tenant retention override: the platform-settings registry supports
tenant-scoped values (scope='tenant'), but no tenant-level retention override
is registered today — the sweep applies the platform default to all tenants.
A tenant-specific override would require an explicit setting like
``workflow_memory.timeseries_retention_days.<tenant_id>`` with no cheap
bulk lookup in the PSR (no list_tenants_with_setting analogue). Documented
limitation for future enhancement.
"""
from __future__ import annotations

from product_names import themed_logger
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from services import platform_settings as psr

logger = themed_logger(__name__, 'workflows')


# ---------------------------------------------------------------------------
# Audit helpers (module-private)
# ---------------------------------------------------------------------------
# The sweep's outer ``db.commit()`` is the *only* commit
# for the session.  The existing ``log_resource_lifecycle_event`` calls
# ``db.commit()`` internally which would commit the entire session early
# and defeat the per-table savepoint isolation.  Instead we add the audit
# record to the session without committing; the sweep's final commit
# includes it.  On failure the audit event is simply lost (per ISO the
# batch work already succeeded, the audit is a best-effort sidecar).
# ---------------------------------------------------------------------------

_audit_import_error: Optional[str] = None


def _audit_add(
        db: Session,
        tenant_id: int,
        action_str: str,
        actor_user_id: int,
        resource_type: str,
        resource_id: int,
        metadata: Optional[dict] = None,
) -> None:
    """Add a ``PermissionAuditLog`` to the session (does NOT commit).

    The caller's ``sweep()`` will commit it at the end.  If the audit
    module cannot be imported (circular import guard) the sweep continues
    silently.
    """
    global _audit_import_error
    if _audit_import_error is not None:
        return  # import already failed once

    try:
        import audit as _audit_mod  # noqa: F811
        import models as _models_mod  # noqa: F811
    except ImportError as e:
        _audit_import_error = str(e)
        return

    # Only block on import failure — other errors (e.g. invalid action)
    # should propagate so the caller's try/except can handle them.

    try:
        # Isolate the audit write in its own SAVEPOINT. If anything here
        # raises a genuine DB-level error (e.g. during the SELECT in
        # ``_get_previous_hash`` or an autoflush of ``db.add`` below), the
        # ``begin_nested`` context manager rolls back only this SAVEPOINT on
        # exception — never the outer session — so the sweep's own pending
        # UPDATEs and its final ``db.commit()`` are not poisoned.
        with db.begin_nested():
            previous_hash = _audit_mod._get_previous_hash(db, tenant_id)  # noqa: SLF001
            record_data = {
                "tenant_id": tenant_id,
                "action": action_str,
                "actor_user_id": actor_user_id,
                "target_user_id": None,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "permission_level": None,
                "permission_id": None,
                "permission_request_id": None,
                "metadata": dict(metadata or {}),
                "ip_address": None,
                "user_agent": None,
                "previous_hash": previous_hash,
            }
            integrity_hash = _audit_mod._compute_audit_hash(record_data)  # noqa: SLF001

            audit_log = _models_mod.PermissionAuditLog(
                tenant_id=tenant_id,
                action=_models_mod.AuditAction(action_str),
                actor_user_id=actor_user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                audit_metadata=metadata,
                integrity_hash=integrity_hash,
                previous_hash=previous_hash,
            )
            db.add(audit_log)
    except Exception as _e:
        # Audit write failure must NOT abort the sweep — log class name only.
        logger.warning(
            "memory-retention audit write failed: %s %s",
            type(_e).__name__,
            _e,
        )

_KV = "workflow_memory_kv"
_VECTOR = "workflow_memory_vector"
_SERIES = "workflow_memory_timeseries"

# --- Settings registration ----------------------------------------------------

_TIMESERIES_RETENTION_KEY = "workflow_memory.timeseries_retention_days"
_DEFAULT_TIMESERIES_RETENTION = 90

_RETENTION_BATCH_KEY = "workflow_memory.retention_batch"
_DEFAULT_RETENTION_BATCH = 5000

_SWEEP_ENABLED_KEY = "workflow_memory.retention_sweep_enabled"
_DEFAULT_SWEEP_ENABLED = False

_MAX_BATCHES = 20  # cap per table to keep one tick bounded


def _validate_positive_int(key_name: str, value, lo: int, hi: int) -> None:
    """Value must be an int in [lo, hi]."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key_name}: must be an int, got {value!r}")
    if value < lo or value > hi:
        raise ValueError(f"{key_name}: must be {lo}..{hi}, got {value!r}")


def _val_retention_days(value) -> None:
    _validate_positive_int("timeseries_retention_days", value, 1, 3650)


def _val_batch_size(value) -> None:
    _validate_positive_int("retention_batch", value, 100, 100000)


psr.register(
    _TIMESERIES_RETENTION_KEY,
    int,
    _DEFAULT_TIMESERIES_RETENTION,
    validator=_val_retention_days,
    description="expr 9.11 — retention window in days for timeseries rows "
                "(older than N days from now is soft-deleted). Default 90, "
                "range 1..3650.",
    group="workflow_memory",
)

psr.register(
    _RETENTION_BATCH_KEY,
    int,
    _DEFAULT_RETENTION_BATCH,
    validator=_val_batch_size,
    description="expr 9.11 — batch size for the retention sweep UPDATE loop. "
                "Default 5000, range 100..100000.",
    group="workflow_memory",
)

psr.register(
    _SWEEP_ENABLED_KEY,
    bool,
    _DEFAULT_SWEEP_ENABLED,
    description="expr 9.11 — kill switch for the memory-retention sweep. "
                "Default off. Platform-level.",
    group="workflow_memory",
)


# --- Core sweep logic ---------------------------------------------------------


def sweep(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Soft-delete expired / stale workflow-memory rows.

    Kill switch off → ``{"status": "disabled"}`` with no DB mutation.

    Processing order: KV table, timeseries table, vector table. Each table's
    updates run inside their own ``db.begin_nested()`` so a failure on one
    table does NOT affect the others. On ``SQLAlchemyError`` the class name
    is logged and that table is skipped (``None`` in the result dict).

    Max 20 batches per table per call to keep one tick bounded.

    Per-tenant timeseries retention override: not implemented (see module
    docstring). The platform default is applied uniformly to all tenants.

    Returns a dict with per-table soft-delete counts:
        {"kv": int|None, "timeseries": int|None, "vector": 0}

    No DELETE statement anywhere in this module.
    """
    enabled = psr.get_setting(_SWEEP_ENABLED_KEY, db=db)
    if not enabled:
        return {"status": "disabled"}

    now = now or datetime.now(timezone.utc)
    retention_days = psr.get_setting(_TIMESERIES_RETENTION_KEY, db=db)
    batch_size = psr.get_setting(_RETENTION_BATCH_KEY, db=db)

    # Sanitise to defaults on bad values (fail-closed)
    if not isinstance(retention_days, int) or isinstance(retention_days, bool):
        retention_days = _DEFAULT_TIMESERIES_RETENTION
    retention_days = max(1, min(retention_days, 3650))

    if not isinstance(batch_size, int) or isinstance(batch_size, bool):
        batch_size = _DEFAULT_RETENTION_BATCH
    batch_size = max(100, min(batch_size, 100000))

    cutoff = now - timedelta(days=retention_days)

    counts: dict[str, Optional[int]] = {}
    counts["kv"] = _sweep_table_kv(db, now, batch_size)
    counts["timeseries"] = _sweep_table_series(db, cutoff, now, batch_size)
    counts["vector"] = 0  # no TTL on vector rows (spec: untouched)

    db.commit()
    return counts


def _sweep_table_kv(db: Session, now: datetime, batch_size: int) -> Optional[int]:
    """Soft-delete KV rows with expires_at <= now AND deleted_at IS NULL.

    Each batch runs inside its own ``db.begin_nested()`` (SAVEPOINT). On
    error the context manager rolls back only that SAVEPOINT — NOT the full
    session — so work already done by other tables in this same call
    (pending in the outer transaction, committed once at the end of
    ``sweep()``) is preserved.

    After the batch loop an aggregated audit row is written per tenant.
    """
    total = 0
    try:
        tenant_counts: Counter = Counter()
        for _ in range(_MAX_BATCHES):
            with db.begin_nested():
                sql = text(
                    f"""
UPDATE {_KV}
SET deleted_at = :now
WHERE id IN (
    SELECT id FROM {_KV}
    WHERE expires_at IS NOT NULL
      AND expires_at <= :now
      AND deleted_at IS NULL
    LIMIT :batch
)
RETURNING tenant_id
"""
                )
                result = db.execute(sql, {"now": now, "batch": batch_size})
                rows = result.fetchall()
                deleted = len(rows)
            if deleted == 0:
                break
            total += deleted
            for row in rows:
                tenant_counts[row[0]] += 1
        # --- aggregated audit row per tenant --------------------------------
        if total > 0:
            try:
                _emit_kv_audit(db, tenant_counts, now)
            except Exception as _e:
                logger.warning(
                    "KV retention audit write failed: %s",
                    type(_e).__name__,
                )
        return total
    except SQLAlchemyError:
        # The failing `with db.begin_nested()` block already rolled back to
        # its SAVEPOINT on exception exit. Do NOT call db.rollback() here —
        # that is a full-session rollback and would discard the other
        # tables' already-applied (but not yet committed) batches.
        logger.exception("KV retention sweep table failed")
        return None


def _sweep_table_series(
        db: Session, cutoff: datetime,
        now: datetime, batch_size: int) -> Optional[int]:
    """Soft-delete timeseries rows with ts < cutoff AND deleted_at IS NULL.

    Applies the platform default retention window uniformly to all tenants.
    Per-tenant overrides are a documented future enhancement.

    Each batch runs inside its own ``db.begin_nested()`` (SAVEPOINT); on
    error only that SAVEPOINT is rolled back (see ``_sweep_table_kv``).

    After the batch loop an aggregated audit row is written per tenant.
    """
    total = 0
    try:
        tenant_counts: Counter = Counter()
        for _ in range(_MAX_BATCHES):
            with db.begin_nested():
                sql = text(
                    f"""
UPDATE {_SERIES}
SET deleted_at = :now
WHERE id IN (
    SELECT id FROM {_SERIES}
    WHERE ts < :cutoff
      AND deleted_at IS NULL
    LIMIT :batch
)
RETURNING tenant_id
"""
                )
                result = db.execute(sql, {"cutoff": cutoff, "now": now,
                                          "batch": batch_size})
                rows = result.fetchall()
                deleted = len(rows)
            if deleted == 0:
                break
            total += deleted
            for row in rows:
                tenant_counts[row[0]] += 1
        # --- aggregated audit row per tenant --------------------------------
        if total > 0:
            try:
                _emit_series_audit(db, tenant_counts, now, cutoff)
            except Exception as _e:
                logger.warning(
                    "series retention audit write failed: %s",
                    type(_e).__name__,
                )
        return total
    except SQLAlchemyError:
        # SAVEPOINT-only rollback already happened in the `with` block above
        # — no db.rollback() here (would discard other tables' pending work).
        logger.exception("timeseries retention sweep table failed")
        return None


# ---------------------------------------------------------------------------
# Audit emission helpers
# ---------------------------------------------------------------------------

# A system actor to use for automated, user-less operations.  The platform
# admin row (id=1) created by the test conftest serves this role.
_SYSTEM_ACTOR_UID = 1


_CONTENT_RETENTION_EXPIRED = "content_retention_expired"


def _emit_kv_audit(db: Session, by_tenant: Counter, now: datetime) -> None:
    for tenant_id, rows_tombstoned in by_tenant.items():
        _audit_add(
            db,
            tenant_id=tenant_id,
            action_str=_CONTENT_RETENTION_EXPIRED,
            actor_user_id=_SYSTEM_ACTOR_UID,
            resource_type=_KV,
            resource_id=0,
            metadata={
                "table": _KV,
                "rows_tombstoned": rows_tombstoned,
                "now": now.isoformat(),
                "sweep": "workflow_memory_retention",
            },
        )


def _emit_series_audit(
        db: Session, by_tenant: Counter, now: datetime, cutoff: datetime,
) -> None:
    for tenant_id, rows_tombstoned in by_tenant.items():
        _audit_add(
            db,
            tenant_id=tenant_id,
            action_str=_CONTENT_RETENTION_EXPIRED,
            actor_user_id=_SYSTEM_ACTOR_UID,
            resource_type=_SERIES,
            resource_id=0,
            metadata={
                "table": _SERIES,
                "rows_tombstoned": rows_tombstoned,
                "now": now.isoformat(),
                "cutoff": cutoff.isoformat(),
                "sweep": "workflow_memory_retention",
            },
        )
