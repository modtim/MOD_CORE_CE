#!/usr/bin/env python3
"""451 / expr-1.8 -- the THREE persistent-workflow-memory tables.

revision      : 20261012h_workflow_memory_tables
down_revision : 20261012g_autonomous_operator_tables

Design Decision 11 (the internal design notes
~128-135): per-workflow + per-tenant scratchpad with three backing stores --
vector DB (RAG), KV (named state), time-series (per-timestamp metrics). They
back the first-class memory nodes (consumers 9.7-9.11: memory_write_kv /
memory_read_kv / memory_embed_and_index / memory_vector_search /
memory_timeseries_append / memory_timeseries_query), the quota gate (9.9) and
the retention sweep (9.11).

SLUG SCOPING (design risk line ~155): memory is keyed by
``(tenant_id, workflow_slug)`` -- NOT ``workflow_id`` -- so renaming /
re-versioning a workflow preserves its memory. There is no column literally
named ``slug`` on the workflows table; the slug-like column is
``workflows.name`` (models.py:2127 ``name = Column(String(255))``), which is
exactly what the existing slug-keyed lookup already matches on:
``services/sub_workflow_validation.py:110`` resolves a sub-workflow ref's
``workflow_slug`` with ``models.Workflow.name == workflow_slug``. So
``workflow_slug VARCHAR(255)`` here mirrors ``workflows.name`` width-for-width
and is deliberately NOT a FK (a FK would re-point memory on rename/recreate
and would drop it on parent delete -- the opposite of the design).

PER-TENANT QUOTA (open question ~182: per-tenant fixed by an edition setting):
NO column is added to the EXISTING ``tenants`` table (models.py is bind-mounted
live; additive-only migration). lead: quota via aggregate SUM(size_bytes) over
the three tables, or a platform-settings registry tunable
(WORKFLOW_MEMORY_QUOTA_GB is a plan-tier setting, not a tenants column).
Every table carries ``size_bytes BIGINT NOT NULL DEFAULT 0`` so that aggregate
is a plain SUM and the 9.9 gate can fail at dispatch with
failure_reason=memory_quota_exceeded.

SOFT DELETE ONLY (ISO 27001, no hard delete): every table has
``deleted_at TIMESTAMPTZ``; the 9.10 workflow-deletion path and the 9.11
retention sweep stamp it, they never DELETE. That is also why both UNIQUE
indexes are PARTIAL (``WHERE deleted_at IS NULL``): a tombstone must not block
a later re-write of the same key, and a re-write must not collide with its own
tombstone.

pgvector dual path (copied from migrations/20261011a_vector_search_embeddings.py
-- same probe, same downgrade):
  * pgvector present -> ``embedding VECTOR(768) NOT NULL`` + the ivfflat cosine
    ANN index (lists = 100), the shipped target.
  * pgvector absent  -> ``embedding JSONB NOT NULL``, the documented fallback
    the app scores in Python (the app DB is postgres:13, no pgvector contrib).
``content_ref`` is JSONB refs/ids only -- never a raw blob -- so memory rows
never re-materialize a production artifact.

``id`` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT (Postgres
rejects subqueries in DEFAULT). Fully idempotent: CREATE TABLE / CREATE INDEX /
CREATE EXTENSION all IF NOT EXISTS; safe to re-run. Additive only: no drops,
no ALTER on existing tables, no DELETE statements.

DRY-RUN by default; ``--commit`` applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012h_workflow_memory_tables.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_KV = "workflow_memory_kv"
_VECTOR = "workflow_memory_vector"
_SERIES = "workflow_memory_timeseries"
_DIM = 768

_PROBE_PGVECTOR = "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"


def _sql_statements(has_pgvector: bool) -> list[tuple[str, str]]:
    """Ordered (label, sql) steps; idempotent, branch on the backend."""
    embedding = (f"embedding VECTOR({_DIM}) NOT NULL"
                 if has_pgvector else "embedding JSONB NOT NULL")
    steps = []
    if has_pgvector:
        steps.append((
            "pgvector contrib: CREATE EXTENSION vector",
            "CREATE EXTENSION IF NOT EXISTS vector;",
        ))
    steps.append((
        f"{_KV} table (expr-1.8, named-state scratchpad)",
        f"""
CREATE TABLE IF NOT EXISTS {_KV} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    workflow_slug VARCHAR(255) NOT NULL,
    key VARCHAR(512) NOT NULL,
    value JSONB NOT NULL,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);
""",
    ))
    steps.append((
        f"{_KV} partial UNIQUE (tenant_id, workflow_slug, key) live-only",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_KV}_live
    ON {_KV} (tenant_id, workflow_slug, key)
    WHERE deleted_at IS NULL;
""",
    ))
    steps.append((
        f"{_VECTOR} table (expr-1.8, embedding-indexed RAG store)",
        f"""
CREATE TABLE IF NOT EXISTS {_VECTOR} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    workflow_slug VARCHAR(255) NOT NULL,
    collection VARCHAR(128) NOT NULL DEFAULT 'default',
    doc_id VARCHAR(512) NOT NULL,
    {embedding},
    content_ref JSONB,
    metadata JSONB,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
""",
    ))
    steps.append((
        f"{_VECTOR} partial UNIQUE (tenant_id, workflow_slug, collection, "
        "doc_id) live-only",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_VECTOR}_live
    ON {_VECTOR} (tenant_id, workflow_slug, collection, doc_id)
    WHERE deleted_at IS NULL;
""",
    ))
    steps.append((
        f"{_SERIES} table (expr-1.8, per-timestamp metric store)",
        f"""
CREATE TABLE IF NOT EXISTS {_SERIES} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    workflow_slug VARCHAR(255) NOT NULL,
    series VARCHAR(255) NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    value DOUBLE PRECISION,
    tags JSONB,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
""",
    ))
    steps.append((
        f"{_SERIES} index (tenant_id, workflow_slug, series, ts DESC)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_SERIES}_series_ts
    ON {_SERIES} (tenant_id, workflow_slug, series, ts DESC);
""",
    ))
    if has_pgvector:
        steps.append((
            f"{_VECTOR} ivfflat cosine index (lists=100)",
            f"""
CREATE INDEX IF NOT EXISTS ix_{_VECTOR}_embedding_ivfflat
    ON {_VECTOR} (embedding vector_cosine_ops)
    WITH (lists = 100);
""",
        ))
    return steps


def _apply(execute, has_pgvector: bool) -> list[str]:
    """Run every step through `execute(sql)`; return the labels applied.

    Kept separate from `main()` so the test can drive it with the conftest
    `db_session` (never the live DB engine).
    """
    applied = []
    for label, sql in _sql_statements(has_pgvector):
        execute(sql)
        applied.append(label)
    return applied


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the CREATE TABLE + index steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for has_pv in (True, False):
            for label, sql in _sql_statements(has_pv):
                print(f"DRY  would: {label}")
                print(sql.rstrip())
        print("DRY  probe: " + _PROBE_PGVECTOR
              + " (picks the backend at apply time)")
        print("DRY  downgrade: no-op (memory is append-only with soft "
              "deletes only; the retention sweep 9.11 stamps deleted_at, "
              "never a DELETE)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    try:
        cur.execute(_PROBE_PGVECTOR)
        has_pv = bool(cur.fetchall())
    except Exception as e:  # noqa: BLE001
        print(f"FAIL pgvector probe: {e}")
        return 1
    if has_pv:
        # `pg_available_extensions` only means the contrib files are
        # installed on the server, not that THIS role may CREATE EXTENSION
        # (that needs superuser on pg13 unless the extension is marked
        # trusted). Probe it for real; a permission failure downgrades to
        # the JSONB backend instead of aborting half-applied.
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        except Exception as e:  # noqa: BLE001
            print(f"(CREATE EXTENSION vector denied to this role: {e}; "
                  "falling back to JSONB backend)")
            has_pv = False
    print(f"backend: {'pgvector VECTOR(768)' if has_pv else 'JSONB fallback'}")
    try:
        _apply(cur.execute, has_pv)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL apply: {e}")
        return 1
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "expr-1.8 ready: workflow_memory_kv / workflow_memory_vector / "
        "workflow_memory_timeseries exist; the memory node handlers (9.7-9.11) "
        "can write and read (tenant_id, workflow_slug) memory, the quota gate "
        "(9.9) sums size_bytes, and the retention sweep (9.11) soft-deletes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
