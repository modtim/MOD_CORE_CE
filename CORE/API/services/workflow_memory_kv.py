"""466 / expr 9.8 (tasks.md 9.8) — workflow-memory KV read/write/delete ops.

Backs the first-class memory nodes ``memory_write_kv`` / ``memory_read_kv``
against ``workflow_memory_kv`` (expr 1.8,
migrations/20261012h_workflow_memory_tables.py).  The table is NOT in
models.py, so this module speaks plain ``text()`` SQL only — no ORM model, no
models.py edit.  Memory is keyed by ``(tenant_id, workflow_slug)`` (design.md
slug-scoping: renaming / re-versioning a workflow preserves its memory), and
every statement here is tenant-scoped in SQL.

Quota (9.9): the gate lives in services/workflow_memory_quota.py, which this
module only IMPORTS (it is under review, so it is not edited here).  A write
reserves headroom on the NET DELTA — new row size minus the existing live row's
size — because an overwrite replaces the old row rather than adding a second
one.  A refusal raises :class:`MemoryQuotaExceeded` carrying the
``QuotaDecision`` (numbers only, never the value text) so the caller can stamp
``failure_reason=memory_quota_exceeded`` at dispatch.

Upsert strategy: the migration's UNIQUE index is PARTIAL
(``uq_workflow_memory_kv_live ... WHERE deleted_at IS NULL``), so the ON
CONFLICT target must repeat that predicate — a tombstone row must not block a
re-write of the same key, and a re-write must not collide with its own
tombstone.  A tombstone is therefore a NEW live row, not an update.

Soft delete only (ISO 27001, no hard delete): ``delete_kv`` stamps
``deleted_at = NOW()``; there is no DELETE statement in this module.

Logging: exception CLASS NAMES only, never values, never the key text, never
the DSN.
"""
from __future__ import annotations

import json
from product_names import themed_logger
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.workflow_memory_quota import (
    QuotaDecision,
    check_memory_write,
    tenant_memory_used_bytes,
    tenant_quota_bytes,
)

logger = themed_logger(__name__, 'workflows')

_TABLE = "workflow_memory_kv"

# Column widths from the migration: key VARCHAR(512), workflow_slug VARCHAR(255).
MAX_KEY_CHARS = 512
MAX_SLUG_CHARS = 255

# list_keys is bounded: the caller cannot ask for an unbounded scan.
MAX_LIST_LIMIT = 1000
DEFAULT_LIST_LIMIT = 100

# Control chars are rejected in a key: a key is a name, not a payload.
# C0 (incl. NUL, newline, tab) plus DEL and the C1 range.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# LIKE escaping: a literal % or _ in a prefix must match literally, so it is
# escaped and an explicit ESCAPE clause pins the escape character.
_LIKE_ESCAPE = "\\"


class MemoryQuotaExceeded(Exception):
    """A KV write was refused by the 9.9 quota gate.

    Carries the QuotaDecision (used / incoming / quota numbers) — never the
    value text, so the exception can be logged or surfaced without echoing
    tenant data.
    """

    def __init__(self, decision: QuotaDecision):
        self.decision = decision
        super().__init__(
            "workflow memory quota exceeded "
            f"(used={decision.used_bytes}, incoming={decision.incoming_bytes}, "
            f"quota={decision.quota_bytes}, reason={decision.reason})")


class InvalidMemoryKey(ValueError):
    """Key is empty, wider than the column, or contains control characters."""


def _json_size(value: Any) -> int:
    """UTF-8 byte length of the compact JSON form stored in the JSONB column.

    Raises ValueError (not TypeError) for a non-JSON-serializable value so the
    memory node gets one honest error type.
    """
    try:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        logger.warning("kv value not JSON-serializable: %s", exc.__class__.__name__)
        raise ValueError("value is not JSON-serializable") from exc
    return len(encoded)


def validate_key(key: Any) -> str:
    """Return the key if it is a usable name, else raise InvalidMemoryKey."""
    if not isinstance(key, str) or not key:
        raise InvalidMemoryKey("key must be a non-empty string")
    if len(key) > MAX_KEY_CHARS:
        raise InvalidMemoryKey(f"key exceeds column width {MAX_KEY_CHARS}")
    if _CONTROL_RE.search(key):
        raise InvalidMemoryKey("key contains control characters")
    return key


def _validate_slug(workflow_slug: Any) -> str:
    if not isinstance(workflow_slug, str) or not workflow_slug:
        raise ValueError("workflow_slug must be a non-empty string")
    if len(workflow_slug) > MAX_SLUG_CHARS:
        raise ValueError(f"workflow_slug exceeds column width {MAX_SLUG_CHARS}")
    return workflow_slug


def _escape_like(prefix: str) -> str:
    return prefix.replace(_LIKE_ESCAPE, _LIKE_ESCAPE + _LIKE_ESCAPE) \
                 .replace("%", _LIKE_ESCAPE + "%") \
                 .replace("_", _LIKE_ESCAPE + "_")


def _existing_live_size(db: Session, tenant_id: int, workflow_slug: str,
                       key: str) -> Optional[int]:
    """size_bytes of the live row for this key, or None when there is none.

    A tombstone does not count: the quota sums live rows only, and an overwrite
    of a tombstone is a fresh insert.
    """
    row = db.execute(
        text(f"SELECT size_bytes FROM {_TABLE} "
             "WHERE tenant_id = :t AND workflow_slug = :s AND key = :k "
             "AND deleted_at IS NULL"),
        {"t": int(tenant_id), "s": workflow_slug, "k": key},
    ).fetchone()
    return None if row is None else int(row[0])


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Bound-param guard for expires_at: tz-aware UTC, or None.

    A naive datetime is interpreted as UTC; a tz-aware one is restamped onto
    UTC so the column never mixes offsets.  Anything else is a caller bug and
    raises TypeError before any SQL runs.
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"expires_at must be datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def write_kv(db: Session, *, tenant_id: int, workflow_slug: str, key: str,
             value: Any, expires_at: Optional[datetime] = None) -> dict:
    """Upsert one live KV row; returns {key, size_bytes, created: bool}.

    ``expires_at`` (655) stamps the row's ``expires_at`` TIMESTAMPTZ column — a
    tz-aware UTC datetime, coerced from a naive one when needed.  The retention
    sweep (services/workflow_memory_retention.py) soft-deletes KV rows whose
    ``expires_at <= now``, so this is what makes a memory row expire at all.

    The ON CONFLICT branch sets ``expires_at = EXCLUDED.expires_at``, so a
    rewrite WITHOUT ttl CLEARS an old expiry: the row's lifetime always follows
    the most recent write, never an expiry an earlier write left behind.

    ``created`` is True when this write opened a NEW live row (fresh key, or a
    rewrite after a soft delete) and False when it updated the row in place —
    the in-place case keeps exactly one live row for the key.

    Quota is checked on the NET DELTA before anything is written, so a refusal
    leaves no row change.  A shrinking rewrite (negative delta) is allowed: the
    shrink frees headroom, so the check runs on the already-reduced usage.
    """
    key = validate_key(key)
    workflow_slug = _validate_slug(workflow_slug)
    size = _json_size(value)

    existing = _existing_live_size(db, tenant_id, workflow_slug, key)
    delta = size - (existing or 0)
    used = tenant_memory_used_bytes(db, tenant_id)
    if delta < 0:
        # Shrink: usage already reflects the replacement; the pure gate refuses
        # a negative raw size, so the delta is expressed as reduced usage.
        used = used + delta
        incoming = 0
    else:
        incoming = delta
    decision = check_memory_write(used, incoming, tenant_quota_bytes(db, tenant_id))
    if not decision.allowed:
        logger.warning("kv write refused: %s", decision.reason)
        raise MemoryQuotaExceeded(decision)

    params = {
        "t": int(tenant_id),
        "s": workflow_slug,
        "k": key,
        "v": json.dumps(value, separators=(",", ":")),
        "size": size,
        "expires_at": _as_utc(expires_at),
    }
    # ON CONFLICT target must match the PARTIAL unique index predicate
    # (uq_workflow_memory_kv_live, WHERE deleted_at IS NULL).
    sql = text(
        f"""
INSERT INTO {_TABLE} (tenant_id, workflow_slug, key, value, size_bytes, expires_at)
VALUES (:t, :s, :k, CAST(:v AS jsonb), :size, :expires_at)
ON CONFLICT (tenant_id, workflow_slug, key) WHERE deleted_at IS NULL
DO UPDATE SET value = EXCLUDED.value,
              size_bytes = EXCLUDED.size_bytes,
              expires_at = EXCLUDED.expires_at,
              updated_at = NOW()
RETURNING (xmax = 0) AS created
"""
    )
    try:
        with db.begin_nested():
            row = db.execute(sql, params).fetchone()
    except Exception as exc:  # noqa: BLE001 — class name only, never the value
        logger.warning("kv write failed: %s", exc.__class__.__name__)
        raise
    created = bool(row and row[0])
    db.commit()
    return {"key": key, "size_bytes": size, "created": created}


def read_kv(db: Session, *, tenant_id: int, workflow_slug: str,
            key: str) -> Any:
    """Live-row value for one tenant + slug, or None.

    Tenant-scoped in SQL; a soft-deleted row is invisible (returns None), and
    another tenant's row is never readable here.
    """
    key = validate_key(key)
    row = db.execute(
        text(f"SELECT value FROM {_TABLE} "
             "WHERE tenant_id = :t AND workflow_slug = :s AND key = :k "
             "AND deleted_at IS NULL"),
        {"t": int(tenant_id), "s": workflow_slug, "k": key},
    ).fetchone()
    return None if row is None else row[0]


def delete_kv(db: Session, *, tenant_id: int, workflow_slug: str,
              key: str) -> bool:
    """SOFT delete only (ISO 27001): stamps deleted_at, never DELETEs.

    Returns True when a live row was tombstoned, False when there was none.
    """
    key = validate_key(key)
    rowcount = db.execute(
        text(f"UPDATE {_TABLE} SET deleted_at = NOW() "
             "WHERE tenant_id = :t AND workflow_slug = :s AND key = :k "
             "AND deleted_at IS NULL"),
        {"t": int(tenant_id), "s": workflow_slug, "k": key},
    ).rowcount
    db.commit()
    return bool(rowcount)


def list_keys(db: Session, *, tenant_id: int, workflow_slug: str,
              prefix: str = "", limit: int = DEFAULT_LIST_LIMIT) -> list[str]:
    """Live keys under one tenant + slug, prefix-matched, bounded by limit.

    ``%`` and ``_`` in the prefix are escaped so they match literally (a prefix
    is data, not a pattern).  limit is clamped to MAX_LIST_LIMIT.
    """
    if not isinstance(prefix, str):
        raise ValueError("prefix must be a string")
    limit = int(limit)
    if limit < 1:
        limit = 1
    if limit > MAX_LIST_LIMIT:
        limit = MAX_LIST_LIMIT
    rows = db.execute(
        text(f"SELECT key FROM {_TABLE} "
             "WHERE tenant_id = :t AND workflow_slug = :s AND deleted_at IS NULL "
             "AND key LIKE :prefix ESCAPE '\\' "
             "ORDER BY key LIMIT :limit"),
        {"t": int(tenant_id), "s": workflow_slug,
         "prefix": _escape_like(prefix) + "%", "limit": limit},
    ).fetchall()
    return [row[0] for row in rows]
