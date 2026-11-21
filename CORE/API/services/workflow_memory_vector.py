"""478 / expr 9.8 (tasks.md 9.8, vector subset) — workflow-memory vector ops.

Backs the ``memory_write_vector`` / ``memory_search_vector`` nodes on the
``workflow_memory_vector`` table (migration 20261012h, NOT in models.py — this
module speaks the table with ``text()`` SQL only).  Column contract (read from
the migration, lines ~118-131):

    id BIGSERIAL PK | tenant_id INT FK | workflow_slug VARCHAR(255)
    collection VARCHAR(128) DEFAULT 'default' | doc_id VARCHAR(512)
    embedding VECTOR(768) when pgvector is available, else JSONB
    content_ref JSONB | metadata JSONB | size_bytes BIGINT | created_at | deleted_at

Backend probe: ``_backend(db)`` reads information_schema.columns udt_name for
the embedding column and caches the answer per process.  pgvector rows are bound
as the text literal ``'[f1,...]'`` cast to vector; the JSONB fallback is cast to
jsonb.  Search ranks by cosine distance on both backends — SQL ``<=>`` on
pgvector, Python cosine on the JSONB fallback (bounded by the scan cap).

Embedding GENERATION is not this task: the caller supplies the 768 floats.

Quota (9.9): services/workflow_memory_quota.py, imported and never edited.  A
write reserves headroom on the NET DELTA (new row size minus the existing live
row's size), because an overwrite replaces the old row rather than adding a
second one.  Refusal raises :class:`MemoryQuotaExceeded` carrying the
QuotaDecision (numbers only, never the embedding or the ref text).

size_bytes = 768 * 4 (the embedding payload) + the compact-JSON byte lengths of
content_ref and metadata.

Upsert strategy: the migration's UNIQUE index is PARTIAL
(``uq_workflow_memory_vector_live ... WHERE deleted_at IS NULL``), so the ON
CONFLICT target repeats that predicate verbatim — a tombstone must not block a
re-write of the same doc, and a re-write must not collide with its own tombstone.

Soft delete only (ISO 27001, no hard delete): ``retire`` stamps ``deleted_at``;
there is no DELETE statement in this module.  Nothing here commits — the caller
owns the transaction.

Logging: exception CLASS NAMES only, never values, never the doc_id text, never
the DSN.
"""
from __future__ import annotations

import json
from product_names import themed_logger
import math
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from services import platform_settings as psr
from services.workflow_memory_quota import (
    QuotaDecision,
    check_memory_write,
    tenant_memory_used_bytes,
    tenant_quota_bytes,
)

logger = themed_logger(__name__, 'workflows')

_TABLE = "workflow_memory_vector"

DIM = 768
EMBED_BYTES = DIM * 4

# Column widths from the migration: collection VARCHAR(128), doc_id VARCHAR(512),
# workflow_slug VARCHAR(255).
MAX_COLLECTION_CHARS = 128
MAX_DOC_ID_CHARS = 512
MAX_SLUG_CHARS = 255

MAX_K = 100
DEFAULT_K = 10

SCAN_CAP_KEY = "workflow_memory.vector_scan_cap"
DEFAULT_SCAN_CAP = 5000

# Control chars are rejected in a name (slug / collection / doc_id): a name is a
# name, not a payload.  C0 (incl. NUL, newline, tab) plus DEL and the C1 range.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Per-process cache of the embedding column's storage type.
_BACKEND_CACHE: Optional[str] = None


class MemoryQuotaExceeded(Exception):
    """A vector write was refused by the 9.9 quota gate.

    Carries the QuotaDecision (used / incoming / quota numbers) — never the
    embedding, so the exception can be logged or surfaced without echoing
    tenant data.
    """

    def __init__(self, decision: QuotaDecision):
        self.decision = decision
        super().__init__(
            "workflow memory quota exceeded "
            f"(used={decision.used_bytes}, incoming={decision.incoming_bytes}, "
            f"quota={decision.quota_bytes}, reason={decision.reason})")


class InvalidMemoryDocId(ValueError):
    """doc_id is empty, wider than the column, or contains control characters."""


def _json_size(value: Any) -> int:
    """UTF-8 byte length of the compact JSON form stored in the JSONB column.

    None (column absent) counts as zero.  Raises ValueError (not TypeError) for
    a non-JSON-serializable value so the memory node gets one honest error type.
    """
    if value is None:
        return 0
    try:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        logger.warning("vector json column not JSON-serializable: %s",
                       exc.__class__.__name__)
        raise ValueError("value is not JSON-serializable") from exc
    return len(encoded)


def _validate_name(value: Any, what: str, limit: int, exc: type) -> str:
    if not isinstance(value, str) or not value:
        raise exc(f"{what} must be a non-empty string")
    if len(value) > limit:
        raise exc(f"{what} exceeds column width {limit}")
    if _CONTROL_RE.search(value):
        raise exc(f"{what} contains control characters")
    return value


def validate_doc_id(doc_id: Any) -> str:
    return _validate_name(doc_id, "doc_id", MAX_DOC_ID_CHARS, InvalidMemoryDocId)


def _validate_slug(workflow_slug: Any) -> str:
    return _validate_name(workflow_slug, "workflow_slug", MAX_SLUG_CHARS, ValueError)


def _validate_collection(collection: Any) -> str:
    return _validate_name(collection, "collection", MAX_COLLECTION_CHARS, ValueError)


def _validate_embedding(embedding: Any) -> list[float]:
    """Exactly DIM finite numbers; bool rejected, NaN/inf rejected."""
    if not isinstance(embedding, (list, tuple)):
        raise ValueError(f"embedding must be a list of {DIM} numbers")
    if len(embedding) != DIM:
        raise ValueError(f"embedding must have {DIM} dimensions, got {len(embedding)}")
    out = []
    for item in embedding:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("embedding entries must be numbers")
        f = float(item)
        if math.isnan(f) or math.isinf(f):
            raise ValueError("embedding entries must be finite")
        out.append(f)
    return out


def _vector_literal(embedding: list[float]) -> str:
    """pgvector text literal: '[f1,f2,...]'."""
    return "[" + ",".join(repr(f) for f in embedding) + "]"


def _backend(db: Session) -> str:
    """'pgvector' or 'jsonb' — the embedding column's actual storage type.

    Probed once per process from information_schema (the table is not in
    models.py), then cached; the answer must not change under a live process.
    """
    global _BACKEND_CACHE
    if _BACKEND_CACHE is not None:
        return _BACKEND_CACHE
    row = db.execute(
        text("SELECT udt_name FROM information_schema.columns "
             "WHERE table_name = :tbl AND column_name = 'embedding'"),
        {"tbl": _TABLE},
    ).fetchone()
    if row is None:
        raise ValueError(f"{_TABLE}.embedding not found")
    _BACKEND_CACHE = "pgvector" if str(row[0]) == "vector" else "jsonb"
    return _BACKEND_CACHE


def _scan_cap(db: Session, tenant_id: Optional[int] = None) -> int:
    """Registered JSONB scan cap, fail-closed to the default on a broken setting."""
    try:
        value = psr.get_setting(SCAN_CAP_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — fail-closed on a broken settings layer
        logger.warning("vector scan-cap setting unreadable; using default")
        return DEFAULT_SCAN_CAP
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 100000:
        return DEFAULT_SCAN_CAP
    return int(value)


def _validate_scan_cap(value) -> None:
    """Honest-range rule for the JSONB scan cap: 1..100000."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"must be an int, got {value!r}")
    if value < 1 or value > 100000:
        raise ValueError(f"must be 1..100000, got {value!r}")


psr.register(
    SCAN_CAP_KEY,
    int,
    DEFAULT_SCAN_CAP,
    validator=_validate_scan_cap,
    description="expr 9.8 (tasks.md 9.8) — maximum number of workflow_memory_vector "
                "rows one JSONB-backend search may load before ranking in Python. "
                "Default 5000; 1..100000. No env var.",
    group="workflow_memory",
)


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cosine similarity, matching pgvector's <=> operator.

    A zero-norm vector has no direction, so its distance is defined as 1.0 (the
    maximum), which keeps the ranking stable instead of dividing by zero.
    """
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


def _existing_live_size(db: Session, tenant_id: int, workflow_slug: str,
                       collection: str, doc_id: str) -> Optional[int]:
    """size_bytes of the live row for this doc, or None when there is none.

    A tombstone does not count: the quota sums live rows only, and an overwrite
    of a tombstone is a fresh insert.
    """
    row = db.execute(
        text(f"SELECT size_bytes FROM {_TABLE} "
             "WHERE tenant_id = :t AND workflow_slug = :s AND collection = :c "
             "AND doc_id = :d AND deleted_at IS NULL"),
        {"t": int(tenant_id), "s": workflow_slug, "c": collection, "d": doc_id},
    ).fetchone()
    return None if row is None else int(row[0])


def upsert_vector(db: Session, *, tenant_id: int, workflow_slug: str,
                  collection: str, doc_id: str, embedding: list[float],
                  content_ref: Any = None, metadata: Any = None) -> dict:
    """Upsert one live vector row; returns the row summary with ``created``.

    ``created`` is True when this write opened a NEW live row (fresh doc, or a
    rewrite after a soft delete) and False when it updated the row in place —
    the in-place case keeps exactly one live row for the doc.

    Validation order (the contract): slug/collection/doc_id shape, embedding
    shape, size, then the quota gate on the NET DELTA, then the INSERT inside a
    SAVEPOINT.  A quota refusal raises MemoryQuotaExceeded and writes nothing.
    """
    workflow_slug = _validate_slug(workflow_slug)
    collection = _validate_collection(collection)
    doc_id = validate_doc_id(doc_id)
    vec = _validate_embedding(embedding)

    size = EMBED_BYTES + _json_size(content_ref) + _json_size(metadata)
    existing = _existing_live_size(db, tenant_id, workflow_slug, collection, doc_id)
    delta = size - (existing or 0)
    used = tenant_memory_used_bytes(db, tenant_id)
    if delta < 0:
        # Shrink: usage already reflects the replacement; the pure gate refuses a
        # negative raw size, so the delta is expressed as reduced usage.
        used = used + delta
        incoming = 0
    else:
        incoming = delta
    decision = check_memory_write(used, incoming, tenant_quota_bytes(db, tenant_id))
    if not decision.allowed:
        logger.warning("vector write refused: %s", decision.reason)
        raise MemoryQuotaExceeded(decision)

    backend = _backend(db)
    cast = "CAST(:e AS vector)" if backend == "pgvector" else "CAST(:e AS jsonb)"
    literal = _vector_literal(vec) if backend == "pgvector" \
        else json.dumps(vec, separators=(",", ":"))
    params = {
        "t": int(tenant_id),
        "s": workflow_slug,
        "c": collection,
        "d": doc_id,
        "e": literal,
        "ref": json.dumps(content_ref, separators=(",", ":")) if content_ref is not None else None,
        "md": json.dumps(metadata, separators=(",", ":")) if metadata is not None else None,
        "size": size,
    }
    # ON CONFLICT target must match the PARTIAL unique index predicate
    # (uq_workflow_memory_vector_live, WHERE deleted_at IS NULL).
    sql = text(
        f"""
INSERT INTO {_TABLE} (tenant_id, workflow_slug, collection, doc_id, embedding,
                      content_ref, metadata, size_bytes)
VALUES (:t, :s, :c, :d, {cast}, :ref, :md, :size)
ON CONFLICT (tenant_id, workflow_slug, collection, doc_id) WHERE deleted_at IS NULL
DO UPDATE SET embedding = EXCLUDED.embedding,
              content_ref = EXCLUDED.content_ref,
              metadata = EXCLUDED.metadata,
              size_bytes = EXCLUDED.size_bytes
RETURNING (xmax = 0) AS created
"""
    )
    try:
        with db.begin_nested():
            row = db.execute(sql, params).fetchone()
    except Exception as exc:  # noqa: BLE001 — class name only, never the value
        logger.warning("vector write failed: %s", exc.__class__.__name__)
        raise
    created = bool(row and row[0])
    return {"doc_id": doc_id, "collection": collection,
            "size_bytes": size, "backend": backend, "created": created}


def search(db: Session, *, tenant_id: int, workflow_slug: str, collection: str,
           query_embedding: list[float], k: int = DEFAULT_K) -> list[dict]:
    """Nearest ``k`` live docs for one tenant + slug + collection.

    Output shape is identical on both backends: [{doc_id, content_ref, metadata,
    distance}], nearest first, cosine distance (1 - cosine similarity).

    pgvector: SQL ``ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k``, which
    the ivfflat cosine index can serve.  JSONB fallback: load at most the
    registered scan cap of live rows, then rank by cosine distance in Python —
    the cap bounds the scan so a JSONB store cannot turn into an unbounded read.

    Live rows only, tenant-scoped IN the SQL, never filtered in Python.
    """
    workflow_slug = _validate_slug(workflow_slug)
    collection = _validate_collection(collection)
    query = _validate_embedding(query_embedding)
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive int")
    if k > MAX_K:
        raise ValueError(f"k exceeds {MAX_K}")

    backend = _backend(db)
    if backend == "pgvector":
        rows = db.execute(
            text(f"""
SELECT doc_id, content_ref, metadata,
       embedding <=> CAST(:q AS vector) AS distance
FROM {_TABLE}
WHERE tenant_id = :t AND workflow_slug = :s AND collection = :c
  AND deleted_at IS NULL
ORDER BY embedding <=> CAST(:q AS vector)
LIMIT :k
"""),
            {"t": int(tenant_id), "s": workflow_slug, "c": collection,
             "q": _vector_literal(query), "k": int(k)},
        ).all()
        return [{"doc_id": r[0], "content_ref": r[1], "metadata": r[2],
                 "distance": float(r[3])} for r in rows]

    # Rank needs the whole bounded scan, not just the first k rows: the nearest
    # k can sit anywhere in the cap, so the LIMIT is the cap itself.
    cap = _scan_cap(db, tenant_id)
    rows = db.execute(
        text(f"""
SELECT doc_id, content_ref, metadata, embedding
FROM {_TABLE}
WHERE tenant_id = :t AND workflow_slug = :s AND collection = :c
  AND deleted_at IS NULL
ORDER BY embedding
LIMIT :limit
"""),
        {"t": int(tenant_id), "s": workflow_slug, "c": collection,
         "limit": int(cap)},
    ).all()
    scored = []
    for r in rows:
        stored = r[3]
        if not isinstance(stored, list):
            logger.warning("vector row is not a list: %s",
                           stored.__class__.__name__)
            continue
        scored.append(({"doc_id": r[0], "content_ref": r[1], "metadata": r[2]},
                       _cosine_distance(query, [float(x) for x in stored])))
    scored.sort(key=lambda item: item[1])
    return [dict(hit, distance=dist) for hit, dist in scored[:k]]


def retire(db: Session, *, tenant_id: int, workflow_slug: str, collection: str,
           doc_id: Optional[str] = None) -> int:
    """SOFT delete only (ISO 27001): stamps deleted_at, never DELETEs.

    With a doc_id, tombstones that one live doc; with doc_id=None, tombstones
    every live doc in the collection.  Returns the number of rows tombstoned.
    """
    workflow_slug = _validate_slug(workflow_slug)
    collection = _validate_collection(collection)
    params = {"t": int(tenant_id), "s": workflow_slug, "c": collection}
    sql = f"UPDATE {_TABLE} SET deleted_at = NOW() " \
          "WHERE tenant_id = :t AND workflow_slug = :s AND collection = :c " \
          "AND deleted_at IS NULL"
    if doc_id is not None:
        params["d"] = validate_doc_id(doc_id)
        sql += " AND doc_id = :d"
    rowcount = db.execute(text(sql), params).rowcount
    return int(rowcount)
