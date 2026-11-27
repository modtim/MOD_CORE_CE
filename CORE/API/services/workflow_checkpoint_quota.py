"""expr 9.5 (tasks.md 9.5) — per-tenant checkpoint-storage quota gate.

``WorkflowCheckpoint`` (models.py ~3676, table ``workflow_checkpoints``) is the
only place a node's mid-flight state is recorded: ``payload JSONB`` plus a
short ``label``.  The table has NO tenant column — the tenant comes via
``workflow_run_id -> WorkflowRun.tenant_id`` — so the per-tenant measure is a
two-table join, not a WHERE on the checkpoint itself.

There is no ``size_bytes`` column here (unlike the three workflow-memory
tables of expr 1.8, which the memory quota sums), so the measurement is the
real Postgres footprint: ``pg_column_size(payload) + octet_length(label)``.
That is what a quota is supposed to bound — bytes the tenant actually occupies.

Two halves, mirroring services/workflow_memory_quota.py (commit 7eecd0df0):

  1. :func:`check_checkpoint_dispatch` — PURE.  Already-resolved numbers in,
     ``(allowed, reason)`` out.  Refusal is a decision, never an exception:
     the dispatcher stamps ``failure_reason=checkpoint_quota_exceeded``.
     Unlike the memory gate, the boundary here is ``used >= quota`` REFUSES
     (the quota is exclusive: it is the ceiling on *existing* usage, and a
     tenant already at the ceiling gets no new checkpoint).

  2. :func:`tenant_checkpoint_used_bytes` — ONE SQL statement, savepoint-wrapped
     so a failed read cannot poison the caller's transaction.

WHERE THIS IS CALLED: pre-dispatch, for ``long_running`` workflows only — the
dispatch path (a separate task) asks :func:`decide_checkpoint_dispatch` before
it queues the run; on ``allowed=False`` it refuses the dispatch with
``checkpoint_quota_exceeded`` instead of starting a run that would only
checkpoint.  This module deliberately does NOT wire itself in here.

The setting is registered via ``psr.register`` like services/adversary_rate_limit.py;
no env vars, no edit to services/platform_settings.py.
"""
from __future__ import annotations

from product_names import themed_logger
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from services import platform_settings as psr

logger = themed_logger(__name__, 'workflows')

QUOTA_KEY = "expr.checkpoint_quota_bytes_per_tenant"
DEFAULT_QUOTA_BYTES = 1073741824  # 1 GiB
MAX_QUOTA_BYTES = 1099511627776   # 1 TiB
REFUSE_REASON = "checkpoint_quota_exceeded"

# One statement, live rows only on BOTH sides.  WorkflowRun carries the
# SoftDelete mixin: a tombstoned run is no longer the active view, so its
# checkpoints stop counting against the tenant that stopped using them.
# Soft-deleted (TTL-swept) checkpoints stop counting too — the retention sweep
# stamps ``workflow_checkpoints.deleted_at`` rather than deleting the row, and a
# swept checkpoint is no longer usable, so it must not keep eating the quota.
_USED_SQL = text(
    """
SELECT COALESCE(SUM(
    COALESCE(pg_column_size(c.payload), 0)
    + COALESCE(octet_length(c.label), 0)
), 0) AS used_bytes
FROM workflow_checkpoints AS c
JOIN workflow_runs AS r ON r.id = c.workflow_run_id
WHERE r.tenant_id = :t AND r.deleted_at IS NULL
  AND c.deleted_at IS NULL
"""
)


def _validate_quota_bytes(value) -> None:
    """Honest-range rule for the tenant quota: 0..1 TiB.

    0 means *unlimited* (the plan tier that never refuses a checkpoint), which
    is why the floor is 0 rather than 1.  The ceiling is 1 TiB so a typo cannot
    park a tenant at 2^63 bytes.  ``bool`` is rejected: True is not a size.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < 0 or value > MAX_QUOTA_BYTES:
        raise ValueError(f"must be 0..{MAX_QUOTA_BYTES}, got {value!r}")


psr.register(
    QUOTA_KEY,
    int,
    psr._ce_unlimited(DEFAULT_QUOTA_BYTES),
    validator=_validate_quota_bytes,
    description="expr 9.5 (tasks.md 9.5) — per-tenant checkpoint-storage quota, "
                "in bytes, measured as SUM(pg_column_size(payload) + "
                "octet_length(label)) over workflow_checkpoints joined to their "
                "workflow_runs tenant. 0 means UNLIMITED (plan-tier unlimited "
                "checkpoint tenant; the CE default — CE has no quantity caps). "
                "Default 1073741824 (1 GiB) on Paid. A tenant-scope "
                "row can raise/lower it; the plan tier sets the tenant default. "
                "No env var.",
    group="expr",
)


def _fail_closed_default() -> int:
    """Fail-closed default: CE returns the UNLIMITED sentinel (0).

    On CE the registered CE default already is 0, so this always returns 0
    unless the paid default is unreachable (in which case it falls through
    to the paid number — a read error on CE means "unlimited" but on paid
    it means "cap at the default").
    """
    try:
        return int(psr._ce_unlimited(DEFAULT_QUOTA_BYTES))  # noqa: SLF001
    except Exception:  # noqa: BLE001 — fail-closed to the paid number
        return DEFAULT_QUOTA_BYTES


def tenant_checkpoint_quota_bytes(db: Optional[Session] = None,
                                  tenant_id: Optional[int] = None) -> int:
    """Resolve the tenant's quota, fail-closed to the registered default.

    Fail-closed means: a broken settings layer falls back to the registered
    default (1 GiB on Paid; the UNLIMITED sentinel 0 on CE, which is what the
    registered CE default already is — see :func:`_fail_closed_default`),
    never to a value *lower* than the default: an unreadable quota must not
    turn into a free pass on a tier that has a cap.
    """
    try:
        value = psr.get_setting(QUOTA_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — fail-closed on a broken settings layer
        logger.warning("checkpoint quota setting unreadable; using default")
        return _fail_closed_default()
    try:
        _validate_quota_bytes(value)
    except ValueError:
        logger.warning("checkpoint quota setting out of range: %r", value)
        return _fail_closed_default()
    return int(value)


def check_checkpoint_dispatch(used_bytes: int,
                              quota_bytes: Optional[int]) -> tuple[bool, Optional[str]]:
    """Pure quota decision: can this tenant get one more checkpoint dispatch?

    Rules, in the order they are applied (the order is the contract):

      * ``quota_bytes`` None or <= 0 -> ``(True, None)``: unlimited.
      * ``used_bytes`` non-int -> coerced to 0 (the sum is DB-sourced and never
        negative), then decided normally.
      * ``used_bytes >= quota_bytes`` -> ``(False, "checkpoint_quota_exceeded")``.
        Exactly AT quota refuses: the quota bounds existing usage, so a tenant
        sitting on the ceiling gets no further checkpoint-bearing dispatch.
      * otherwise -> ``(True, None)``.
    """
    used = int(used_bytes) if isinstance(used_bytes, int) and not isinstance(used_bytes, bool) else 0
    if quota_bytes is None or not isinstance(quota_bytes, int) or int(quota_bytes) <= 0:
        return (True, None)
    quota = int(quota_bytes)
    if used >= quota:
        return (False, REFUSE_REASON)
    return (True, None)


def tenant_checkpoint_used_bytes(db: Session, tenant_id: int) -> int:
    """Live checkpoint bytes for one tenant (join via ``workflow_runs``).

    The checkpoint table has no tenant column, so the tenant is the run's:
    ``workflow_checkpoints -> workflow_runs.tenant_id``.  Soft-deleted runs do
    not count (ISO 27001: tombstones are not the active view).

    Returns 0 when the read fails — the tables may not exist yet (the lead
    applies the migrations), and a measurement failure must never be read as
    "at quota".  Only ``ProgrammingError`` is caught and only the exception
    *class name* is logged: no SQL, no DSN, no values.

    Postgres aborts the whole surrounding transaction on a failed statement,
    so the query runs inside a SAVEPOINT (``db.begin_nested()``): a failure
    rolls back to that savepoint and the caller's outer transaction, and the
    rest of its session, stays usable.
    """
    try:
        with db.begin_nested():
            row = db.execute(_USED_SQL, {"t": int(tenant_id)}).fetchone()
    except ProgrammingError as exc:  # undefined_table -> pre-migration posture
        logger.warning("checkpoint usage read failed: %s", exc.__class__.__name__)
        return 0
    value = row[0] if row is not None else 0
    return int(value or 0)


def decide_checkpoint_dispatch(db: Session, tenant_id: int) -> dict:
    """Convenience join of the two halves: sum the live usage, then decide.

    Returns a plain dict ``{allowed, reason, used_bytes, quota_bytes}`` — the
    dispatcher stamps ``reason`` as the run's ``failure_reason`` when refusing.
    Never commits; never mutates.
    """
    used = tenant_checkpoint_used_bytes(db, tenant_id)
    quota = tenant_checkpoint_quota_bytes(db, tenant_id)
    allowed, reason = check_checkpoint_dispatch(used, quota)
    return {"allowed": allowed, "reason": reason, "used_bytes": used, "quota_bytes": quota}
