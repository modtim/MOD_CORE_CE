#!/usr/bin/env python3
"""sz-5.4.2 — create the tenant-scoped `vector_search_embeddings` table.

revision      : 20261011a_vector_search_embeddings
down_revision : 20261010j_worker_host_exec_access

`services/ai_primitives/vector_search.py` (sz-5.4.1) deliberately issues NO
DDL: `ensure_vector_table()` only probes for the table and raises a
`RuntimeError` naming this migration when it is absent. This migration is
what lands that documented schema on the app DB, so the primitive stops
raising once it is applied.

Two backends, one contract (mirrors the module's docstring):
  * pgvector present  -> `embedding VECTOR(768) NOT NULL` + the ivfflat
    cosine ANN index (`lists = 100`), the shipped target.
  * pgvector absent   -> `embedding JSONB NOT NULL`, the documented
    pre-migration fallback the module scores in Python (the app DB is
    `postgres:13`, which carries no pgvector contrib). This is exactly the
    column type `store_embeddings` writes JSON text into and
    `tenant_vector_search` reads back through `_as_floats`.

Table (per the module docstring):
    id          BIGSERIAL PRIMARY KEY
    tenant_id   INTEGER NOT NULL REFERENCES tenants (id)
                -- the docstring says BIGINT, but `tenants.id` is INTEGER
                -- (models.py Tenant.id = Column(Integer)); a BIGINT FK
                -- column would not match the target type, so this lands
                -- INTEGER, the same type every other tenant FK in
                -- migrations/ uses. Widening later is a separate
                -- additive migration.
    collection  VARCHAR(100) NOT NULL
    document_id VARCHAR(255) NOT NULL
    embedding   VECTOR(768) NOT NULL | JSONB NOT NULL
    content     TEXT
    metadata    JSONB
    deleted_at  TIMESTAMPTZ            -- ISO 27001 soft delete
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()

Indexes:
    ix_..._tenant_collection   btree on (tenant_id, collection)
                               (every query is tenant+collection WHERE-bound)
    NO unique index on (tenant_id, collection, document_id):
        `store_embeddings` plain-INSERTs (no ON CONFLICT / upsert), so
        re-storing a document_id is a legitimate duplicate row today
        (search returns both). A partial UNIQUE would make that INSERT
        fail, i.e. it would change the primitive's behaviour, so it is
        skipped. If dedupe-on-write ships later it lands with an
        `ON CONFLICT` upsert in the service, in its own migration.

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.
Fully idempotent: CREATE TABLE / CREATE INDEX / CREATE EXTENSION all
`IF NOT EXISTS`; safe to re-run. Additive only: no drops, no ALTER on
existing tables, no DELETE statements (soft delete via `deleted_at`).

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \
        migrations/20261011a_vector_search_embeddings.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "vector_search_embeddings"
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
        f"{_TABLE} table (sz-5.4.2, tenant-scoped embedding corpus)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    collection VARCHAR(100) NOT NULL,
    document_id VARCHAR(255) NOT NULL,
    {embedding},
    content TEXT,
    metadata JSONB,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
    ))
    steps.append((
        f"{_TABLE} btree index on (tenant_id, collection)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_collection
    ON {_TABLE} (tenant_id, collection);
""",
    ))
    if has_pgvector:
        steps.append((
            f"{_TABLE} ivfflat cosine index (lists=100)",
            f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_embedding_ivfflat
    ON {_TABLE} (embedding vector_cosine_ops)
    WITH (lists = 100);
""",
        ))
    return steps


def _apply(execute, has_pgvector: bool) -> list[str]:
    """Run every step through `execute(sql)`; return the labels applied.

    Kept separate from `main()` so the test can drive it with the
    conftest `db_session` (never the live DB engine).
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
        print("DRY  downgrade: no-op (append-only per ISO 27001; rows "
              "soft-delete via deleted_at, never a DELETE)")
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
        f"{_TABLE} ready: services/ai_primitives/vector_search.py "
        "ensure_vector_table() now returns "
        f"{'vector' if has_pv else 'JSONB'} instead of raising."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
