"""expr 9.8 (tasks.md 9.8, timeseries subset) — workflow-memory time-series ops.

Backs the ``memory_timeseries_append`` / ``memory_timeseries_query`` nodes on
the ``workflow_memory_timeseries`` table (migration 20261012h, NOT in models.py
-- this module speaks the table with ``text()`` SQL only).  Column contract
(read from the migration, lines ~143-165):

    id BIGSERIAL PK | tenant_id INT FK | workflow_slug VARCHAR(255)
    series VARCHAR(255) | ts TIMESTAMPTZ | value DOUBLE PRECISION
    tags JSONB | size_bytes BIGINT NOT NULL | created_at | deleted_at

Because ``value`` is DOUBLE PRECISION (not JSONB), a point value MUST be a
finite number; a Mapping point value is rejected -- the migration gives no
JSONB value column to put it in (the vector table's JSONB fallback is a
different table; the KV store, task 466, owns named objects).

Slug scoping mirrors the migration: memory is keyed by
``(tenant_id, workflow_slug)`` so renames preserve memory.

Quota: services/workflow_memory_quota.py (imported, never edited).  The batch
is charged as ONE net delta: used = tenant_memory_used_bytes, incoming = SUM
size_bytes of the batch, via check_memory_write.  Refusal raises
:class:`MemoryQuotaExceeded` -- exception class names only in logs, never
values, never the numbers.

size_bytes per row (documented, matches task 466's "compact JSON + fixed
overhead" convention): ``len(json.dumps(value))`` for the value payload plus
_ROW_OVERHEAD_BYTES fixed per-row overhead (ts, series/slug keying, FKs,
created_at tombstone bookkeeping).  The caller of an OVERWRITE would pass a
net delta; appends are always positive.

SOFT DELETE ONLY (ISO 27001): retire_series stamps ``deleted_at``; this module
contains no DELETE statement at all.

Setting: ``workflow_memory.timeseries_max_batch`` (int, default 5000, 1..100000)
registered via psr.register like services/adversary_rate_limit.py.  No env
vars; services/platform_settings.py is never edited.
"""
from __future__ import annotations

import json
from product_names import themed_logger
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from services import platform_settings as psr
from services.workflow_memory_quota import (
    check_memory_write,
    tenant_memory_used_bytes,
    tenant_quota_bytes,
)

logger = themed_logger(__name__, 'workflows')

_SERIES = "workflow_memory_timeseries"
_SERIES_MAX_LEN = 255          # VARCHAR(255) on the migration
_SLUG_MAX_LEN = 255

MAX_BATCH_KEY = "workflow_memory.timeseries_max_batch"
DEFAULT_MAX_BATCH = 5000
QUERY_LIMIT_MAX = 10000
_ROW_OVERHEAD_BYTES = 64       # fixed per-row cost: ts + keying + timestamps


class MemoryQuotaExceeded(Exception):
    """A memory write was refused by the quota gate.  Carries NO values."""


def _validate_series_name(series) -> None:
    if not isinstance(series, str) or not series:
        raise ValueError("series must be a non-empty string")
    if len(series) > _SERIES_MAX_LEN:
        raise ValueError(f"series too long (>{_SERIES_MAX_LEN})")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in series):
        raise ValueError("series contains control characters")


def _validate_slug(slug) -> None:
    if not isinstance(slug, str) or not slug:
        raise ValueError("workflow_slug must be a non-empty string")
    if len(slug) > _SLUG_MAX_LEN:
        raise ValueError(f"workflow_slug too long (>{_SLUG_MAX_LEN})")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in slug):
        raise ValueError("workflow_slug contains control characters")


def _require_aware(ts, what: str) -> None:
    if not isinstance(ts, datetime):
        raise ValueError(f"{what} must be a datetime")
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{what} must be tz-aware (naive rejected)")


def _point_value(value) -> float:
    """Finite numeric value only; Mapping rejected (value column is DOUBLE)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("point value must be a finite number")
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("point value must be finite")
    return f


def _row_size_bytes(value: float) -> int:
    return len(json.dumps(value)) + _ROW_OVERHEAD_BYTES


def _validate_max_batch(value) -> None:
    """Honest-range rule for the append batch cap: 1..100000."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < 1 or value > 100000:
        raise ValueError(f"must be 1..100000, got {value!r}")


def _max_batch(db: Optional[Session] = None, tenant_id: Optional[int] = None) -> int:
    """Registered batch cap, fail-closed to the default on a broken setting."""
    try:
        value = psr.get_setting(MAX_BATCH_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — fail-closed on a broken settings layer
        logger.warning("timeseries max-batch setting unreadable; using default")
        return DEFAULT_MAX_BATCH
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 100000:
        return DEFAULT_MAX_BATCH
    return int(value)


psr.register(
    MAX_BATCH_KEY,
    int,
    DEFAULT_MAX_BATCH,
    validator=_validate_max_batch,
    description="expr 9.8 (tasks.md 9.8) — maximum number of time-series "
                "points one memory_timeseries_append call may write. "
                "Default 5000; 1..100000. No env var.",
    group="workflow_memory",
)


_APPEND_SQL_TMPL = None  # built per call (VALUES arity varies with batch size)


def append_points(db: Session, *, tenant_id: int,
                  workflow_slug: str, series: str,
                  points: Sequence[tuple]) -> dict:
    """Append (ts, value) points to one series.  Returns {series, appended, size_bytes}.

    Validation order (the contract): slug/series shape, batch cap, every point
    (tz-aware ts, finite numeric value), then the quota gate on the BATCH TOTAL,
    then ONE multi-VALUES INSERT inside a SAVEPOINT.  A quota refusal raises
    MemoryQuotaExceeded and writes nothing.
    """
    _validate_slug(workflow_slug)
    _validate_series_name(series)
    if not isinstance(tenant_id, int) or isinstance(tenant_id, bool):
        raise ValueError("tenant_id must be an int")
    if not isinstance(points, Sequence) or isinstance(points, str):
        raise ValueError("points must be a sequence of (ts, value) tuples")
    cap = _max_batch(db, tenant_id)
    if len(points) > cap:
        raise ValueError(f"batch too large ({len(points)} > cap)")

    rows = []
    batch_bytes = 0
    for item in points:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("each point must be a (ts, value) tuple")
        ts, value = item
        _require_aware(ts, "point ts")
        v = _point_value(value)
        size = _row_size_bytes(v)
        batch_bytes += size
        rows.append((ts, v, size))

    used = tenant_memory_used_bytes(db, tenant_id)
    quota = tenant_quota_bytes(db, tenant_id)
    decision = check_memory_write(used, batch_bytes, quota)
    if not decision.allowed:
        logger.warning("timeseries append refused by quota gate: %s",
                       decision.reason)
        raise MemoryQuotaExceeded("workflow-memory timeseries write refused")

    params = {"tenant_id": int(tenant_id), "slug": workflow_slug,
              "series": series}
    value_clauses = []
    for i, (ts, v, size) in enumerate(rows):
        value_clauses.append(
            f"(:tenant_id, :slug, :series, :ts_{i}, :val_{i}, :sz_{i})")
        params[f"ts_{i}"] = ts
        params[f"val_{i}"] = v
        params[f"sz_{i}"] = size
    sql = text(
        "INSERT INTO " + _SERIES + " "
        "(tenant_id, workflow_slug, series, ts, value, size_bytes) VALUES "
        + ", ".join(value_clauses)
    )
    with db.begin_nested():
        db.execute(sql, params)
    return {"series": series, "appended": len(rows), "size_bytes": batch_bytes}


def query_points(db: Session, *, tenant_id: int, workflow_slug: str,
                 series: str, start: datetime, end: datetime,
                 limit: int = 1000, order: str = "asc") -> list[dict]:
    """Live rows in [start, end) for one (tenant, slug, series), index-ordered.

    The migration's index is (tenant_id, workflow_slug, series, ts DESC); the
    WHERE mirrors that prefix exactly and ORDER BY ts follows the index for
    ``order="desc"`` (``asc`` re-reads the same index backwards).  Tenant-scoped
    IN the SQL, never filtered in Python.  Soft-deleted rows are invisible.
    """
    _validate_slug(workflow_slug)
    _validate_series_name(series)
    _require_aware(start, "start")
    _require_aware(end, "end")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive int")
    if limit > QUERY_LIMIT_MAX:
        raise ValueError(f"limit exceeds {QUERY_LIMIT_MAX}")
    if order not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    if end <= start:
        raise ValueError("window must be non-empty (end > start)")

    sql = text(
        f"""
SELECT ts, value, tags
FROM {_SERIES}
WHERE tenant_id = :tenant_id
  AND workflow_slug = :slug
  AND series = :series
  AND deleted_at IS NULL
  AND ts >= :start
  AND ts < :end
ORDER BY ts {"DESC" if order == "desc" else "ASC"}
LIMIT :limit
"""
    )
    rows = db.execute(sql, {
        "tenant_id": int(tenant_id), "slug": workflow_slug, "series": series,
        "start": start, "end": end, "limit": int(limit),
    }).all()
    return [{"ts": r[0], "value": r[1], "tags": r[2]} for r in rows]


def downsample(points: Sequence[dict], bucket: timedelta,
               agg: str) -> list[dict]:
    """Pure bucketing for node outputs: [{bucket_start, value}].

    Buckets are floor-aligned to the EARLIEST point so the output is stable
    regardless of sort order.  Empty buckets are skipped (only buckets that
    contain at least one point are emitted).  Aggs: mean, min, max, last,
    count.  Input points are dicts with 'ts' (tz-aware) and 'value'; tuples
    (ts, value) are accepted too.
    """
    if not isinstance(bucket, timedelta) or bucket.total_seconds() <= 0:
        raise ValueError("bucket must be a positive timedelta")
    if agg not in ("mean", "min", "max", "last", "count"):
        raise ValueError("agg must be mean|min|max|last|count")
    norm = []
    for p in points:
        if isinstance(p, dict):
            ts, value = p.get("ts"), p.get("value")
        elif isinstance(p, tuple) and len(p) == 2:
            ts, value = p
        else:
            raise ValueError("point must be a dict or (ts, value) tuple")
        _require_aware(ts, "point ts")
        norm.append((ts, _point_value(value)))
    if not norm:
        return []
    origin = min(ts for ts, _ in norm)
    step = bucket.total_seconds()
    groups: dict[int, list[tuple[datetime, float]]] = {}
    for ts, v in sorted(norm, key=lambda x: x[0]):
        idx = int((ts - origin).total_seconds() // step)
        groups.setdefault(idx, []).append((ts, v))
    out = []
    for idx in sorted(groups):
        members = groups[idx]
        values = [v for _, v in members]
        if agg == "mean":
            result = sum(values) / len(values)
        elif agg == "min":
            result = min(values)
        elif agg == "max":
            result = max(values)
        elif agg == "last":
            result = values[-1]
        else:
            result = float(len(values))
        out.append({"bucket_start": origin + timedelta(seconds=idx * step),
                    "value": result})
    return out


def retire_series(db: Session, *, tenant_id: int, workflow_slug: str,
                  series: str) -> int:
    """SOFT delete every live row of one series (deleted_at = NOW()).  ISO 27001.

    Returns the number of rows tombstoned.  There is no DELETE statement
    anywhere in this module; tombstones keep counting nothing against the
    quota (the quota SUM filters deleted_at IS NULL) but stay in the record.
    """
    _validate_slug(workflow_slug)
    _validate_series_name(series)
    sql = text(
        f"""
UPDATE {_SERIES}
SET deleted_at = NOW()
WHERE tenant_id = :tenant_id
  AND workflow_slug = :slug
  AND series = :series
  AND deleted_at IS NULL
RETURNING id
"""
    )
    with db.begin_nested():
        result = db.execute(sql, {
            "tenant_id": int(tenant_id), "slug": workflow_slug,
            "series": series,
        })
        return len(result.fetchall())
