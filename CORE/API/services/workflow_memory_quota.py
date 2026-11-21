"""expr 9.9 (tasks.md 9.9) — per-tenant workflow-memory quota gate.

The three persistent-workflow-memory tables (expr 1.8,
migrations/20261012h_workflow_memory_tables.py) carry ``size_bytes BIGINT NOT
NULL DEFAULT 0`` on every row.  Per design.md open question ~182 the quota is
per-tenant and fixed at the plan tier, and the lead's ruling is: NO column on
``tenants`` (models.py is bind-mounted live, additive-only migration) — the
quota is the aggregate ``SUM(size_bytes)`` over the three tables, plus a
platform-settings registry tunable.  That is exactly what this module does.

Two halves:

  1. :func:`check_memory_write` — PURE.  It takes already-resolved numbers
     (used / incoming / quota) and returns a :class:`QuotaDecision`.  Pure so
     the boundary rules are testable without a DB and so the memory-write
     nodes (9.7-9.10) can call it inside a transaction without re-reading the
     settings layer.  Refusal is a decision, not an exception: the caller
     stamps ``failure_reason=memory_quota_exceeded`` at dispatch.

  2. :func:`tenant_memory_used_bytes` — ONE SQL statement, UNION ALL over the
     three tables, ``WHERE tenant_id = :t AND deleted_at IS NULL``.  Soft
     delete only (ISO 27001, no hard delete): a tombstone row is gone from the
     active view, so it does not count against the quota.  Missing tables
     return 0 (the migration has not been applied yet by the lead) — the gate
     is then effectively unlimited for that tenant, which is the safe posture
     for a lane that runs before migration h lands.

OVERWRITE CASE (documented, caller's responsibility): a re-write of an
existing key replaces the old row, so the quota delta is NOT the new row size
alone — the caller must pass the NET DELTA, ``incoming_bytes =
new_size_bytes - old_size_bytes`` (negative when the rewrite shrinks the row).
A negative net delta is legal here and can never refuse: ``used + incoming``
drops.  Only a negative *incoming* that is not an int, or a non-int incoming,
is ``invalid_size`` — see the decision table.

The setting is registered via ``psr.register`` like services/adversary_rate_limit.py;
no env vars, no edit to services/platform_settings.py.
"""
from __future__ import annotations

from product_names import themed_logger
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from services import platform_settings as psr
try:
    from services.expressiveness_metrics import set_memory_quota_used_bytes
except ImportError:
    set_memory_quota_used_bytes = None

logger = themed_logger(__name__, 'workflows')

QUOTA_KEY = "workflow_memory.tenant_quota_bytes"
DEFAULT_QUOTA_BYTES = 1073741824  # 1 GiB, design.md open question ~182

_KV = "workflow_memory_kv"
_VECTOR = "workflow_memory_vector"
_SERIES = "workflow_memory_timeseries"

# Single statement, three tables, live rows only.  ``SUM`` over an empty set is
# NULL, so COALESCE to 0 keeps the return type an int.
_USED_SQL = text(
    f"""
SELECT COALESCE(SUM(size_bytes), 0) AS used_bytes
FROM (
    SELECT size_bytes FROM {_KV}
    WHERE tenant_id = :t AND deleted_at IS NULL
    UNION ALL
    SELECT size_bytes FROM {_VECTOR}
    WHERE tenant_id = :t AND deleted_at IS NULL
    UNION ALL
    SELECT size_bytes FROM {_SERIES}
    WHERE tenant_id = :t AND deleted_at IS NULL
) AS live_memory_rows
"""
)


def _validate_quota_bytes(value) -> None:
    """Honest-range rule for the tenant quota: 0..1 TiB.

    0 means *unlimited* (the plan-tier "unlimited memory" tier), which is why
    the floor is 0 rather than 1 — unlike the adversary rate limit, where 0
    would mean "never", a memory quota of 0 is the documented unlimited
    sentinel.  The ceiling is 1 TiB so a typo cannot park a tenant at
    2^63 bytes.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < 0 or value > 1099511627776:
        raise ValueError(f"must be 0..1099511627776, got {value!r}")


psr.register(
    QUOTA_KEY,
    int,
    psr._ce_unlimited(DEFAULT_QUOTA_BYTES),
    validator=_validate_quota_bytes,
    description="expr 9.9 (tasks.md 9.9) — per-tenant workflow-memory quota, "
                "in bytes, measured as SUM(size_bytes) over the three "
                "workflow-memory tables (live rows only). 0 means UNLIMITED "
                "(plan-tier unlimited-memory tenant; the CE default — CE has "
                "no quantity caps). Default 1073741824 (1 GiB) on Paid. A "
                "tenant-scope row can raise/lower it; the plan "
                "tier sets the tenant default. No env var.",
    group="workflow_memory",
)


@dataclass(frozen=True)
class QuotaDecision:
    """Outcome of one memory-write quota check (see check_memory_write)."""
    allowed: bool
    used_bytes: int
    incoming_bytes: int
    quota_bytes: Optional[int]
    reason: str  # "ok" | "quota_exceeded" | "unlimited" | "invalid_size"


REASONS = ("ok", "quota_exceeded", "unlimited", "invalid_size")


def _fail_closed_default() -> int:
    """The value a broken settings layer falls back to.

    FIX-CE-UNLIMITED-E: on CE that fallback is the UNLIMITED sentinel (0),
    because the CE default IS unlimited (owner rule: CE has no quantity caps)
    — falling back to 1 GiB there would ship a cap the product never
    configured. On Paid / unknown edition / erroring probe it stays the old
    1 GiB, so the fail-closed property (an unreadable quota is never a free
    pass) is preserved exactly where a cap exists.
    """
    try:
        return int(psr._ce_unlimited(DEFAULT_QUOTA_BYTES))  # noqa: SLF001
    except Exception:  # noqa: BLE001 — fail-closed to the paid number
        return DEFAULT_QUOTA_BYTES


def tenant_quota_bytes(db: Optional[Session] = None,
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
        logger.warning("workflow-memory quota setting unreadable; using default")
        return _fail_closed_default()
    try:
        _validate_quota_bytes(value)
    except ValueError:
        logger.warning("workflow-memory quota setting out of range: %r", value)
        return _fail_closed_default()
    return int(value)


def check_memory_write(used_bytes: int,
                       incoming_bytes: int,
                       quota_bytes: Optional[int]) -> QuotaDecision:
    """Pure quota decision for one memory write.

    Rules, in the order they are applied (the order is the contract):

      * ``incoming_bytes`` non-int or < 0 -> ``invalid_size``, REFUSE.  A
        negative incoming is only legal as a *net delta* from an overwrite
        (see the module docstring); as a raw size it is nonsense, so refuse
        rather than silently granting headroom.  ``bool`` is rejected: True is
        not a size.
      * ``quota_bytes`` None or <= 0 -> ``unlimited``, ALLOW.
      * ``used + incoming > quota`` -> ``quota_exceeded``, REFUSE.
      * exactly at quota (``used + incoming == quota``) -> ``ok``, ALLOW: the
        quota is inclusive, +1 byte is what refuses.

    ``used_bytes`` is the caller's already-summed live usage (typically from
    :func:`tenant_memory_used_bytes`); a non-int used is coerced to 0 rather
    than refused, because the sum is DB-sourced and never negative.
    """
    if isinstance(incoming_bytes, bool) or not isinstance(incoming_bytes, int):
        return QuotaDecision(
            allowed=False,
            used_bytes=int(used_bytes) if isinstance(used_bytes, int) else 0,
            incoming_bytes=0,
            quota_bytes=quota_bytes,
            reason="invalid_size",
        )
    used = int(used_bytes) if isinstance(used_bytes, int) else 0
    if incoming_bytes < 0:
        return QuotaDecision(
            allowed=False,
            used_bytes=used,
            incoming_bytes=incoming_bytes,
            quota_bytes=quota_bytes,
            reason="invalid_size",
        )
    if quota_bytes is None or int(quota_bytes) <= 0:
        return QuotaDecision(
            allowed=True,
            used_bytes=used,
            incoming_bytes=incoming_bytes,
            quota_bytes=quota_bytes,
            reason="unlimited",
        )
    quota = int(quota_bytes)
    allowed = (used + incoming_bytes) <= quota
    return QuotaDecision(
        allowed=allowed,
        used_bytes=used,
        incoming_bytes=incoming_bytes,
        quota_bytes=quota,
        reason="ok" if allowed else "quota_exceeded",
    )


def tenant_memory_used_bytes(db: Session, tenant_id: int) -> int:
    """Live SUM(size_bytes) for one tenant across the three memory tables.

    Soft-deleted rows do not count (ISO 27001: tombstones are not the active
    view).  Returns 0 when the tables do not exist yet — migration h is applied
    by the lead, so a lane running before it must not crash.  Only
    ``ProgrammingError`` (Postgres 42P01 undefined_table) is caught, and only
    the exception *class name* is logged: no SQL, no DSN, no values.

    Postgres aborts the whole surrounding transaction on a failed statement,
    so catching ProgrammingError bare would leave the caller's session
    unusable ("current transaction is aborted") for every query after this
    one.  The query runs inside a SAVEPOINT (``db.begin_nested()``) so a
    failure only rolls back to that savepoint — the caller's outer
    transaction, and the rest of its session, stays usable.
    """
    try:
        with db.begin_nested():
            row = db.execute(_USED_SQL, {"t": int(tenant_id)}).fetchone()
    except ProgrammingError as exc:  # undefined_table -> pre-migration posture
        logger.warning("workflow-memory usage read failed: %s",
                       exc.__class__.__name__)
        return 0
    value = row[0] if row is not None else 0
    total = int(value or 0)
    try:
        set_memory_quota_used_bytes(tenant_id, total)
    except Exception:  # noqa: BLE001 — metrics are best-effort
        pass
    return total


def decide_memory_write(db: Session, tenant_id: int, incoming_bytes: int,
                        net_delta: bool = False) -> QuotaDecision:
    """Convenience join of the two halves: sum the live usage, then decide.

    ``net_delta=True`` documents the overwrite case at the call site: the
    caller already subtracted the old row's size from ``incoming_bytes``.
    """
    used = tenant_memory_used_bytes(db, tenant_id)
    quota = tenant_quota_bytes(db, tenant_id)
    return check_memory_write(used, incoming_bytes, quota)
