#!/usr/bin/env python3
"""sz-5.4.2g — additive ALTER on the already-live vector_search_embeddings.

revision      : 20261011g_vector_search_embeddings
down_revision : 20261011f_workflow_crdt

HISTORY (why this migration is an ALTER, not a CREATE):
  * 20261011a_vector_search_embeddings OWNS the table: it CREATEs
    `vector_search_embeddings` (pgvector VECTOR(768) + ivfflat, or the
    JSONB fallback on postgres:13) plus the btree index on
    (tenant_id, collection). On the live app DB that table already exists.
  * g originally shipped as a second `CREATE TABLE IF NOT EXISTS`. Against
    the live DB that guard makes g a pure no-op, so g added NOTHING: the
    columns the vector_search service soft-deletes/updates toward
    (`deleted_by`, `updated_at`) and the live-only partial index never
    landed. This rewrite makes g do what it was supposed to do.

g is now purely ADDITIVE on a's table:
    ALTER TABLE ... ADD COLUMN IF NOT EXISTS deleted_by INTEGER NULL
        REFERENCES users (id)   -- no ON DELETE action: a's `deleted_at`
                                  stays the soft-delete stamp, `deleted_by`
                                  only records WHO soft-deleted the row
    ALTER TABLE ... ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL
        DEFAULT now()
    CREATE INDEX IF NOT EXISTS ix_vse_tenant_collection_live
        ON vector_search_embeddings (tenant_id, collection)
        WHERE deleted_at IS NULL
        -- skipped when an equivalent index already covers
           (tenant_id, collection); the reused indexname is printed

If the table is absent g refuses to create it: it prints "run 20261011a
first" and exits 1. a owns the schema; two migrations creating one table
is exactly how the original g got its no-op in the first place.

No CREATE TABLE, no DROP, no ALTER of an existing column's type, no
DELETE statements (ISO 27001: rows soft-delete via deleted_at).
Fully idempotent: every statement is IF NOT EXISTS, safe to re-run.
Downgrade is a no-op (append-only per ISO 27001).

DRY-RUN by default (prints the statements, never connects); `--commit`
applies. NEVER applied to the live DB by the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261011g_vector_search_embeddings.py --commit
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_TABLE = "vector_search_embeddings"
_REVISION = "20261011g_vector_search_embeddings"
_DOWN_REVISION = "20261011f_workflow_crdt"
_INDEX_NAME = "ix_vse_tenant_collection_live"
_INDEX_COLS = ("tenant_id", "collection")

_PROBE_TABLE = (f"SELECT to_regclass('public.{_TABLE}') IS NOT NULL;")
_PROBE_INDEXES = (
    "SELECT indexname, indexdef FROM pg_indexes "
    f"WHERE schemaname = 'public' AND tablename = '{_TABLE}';"
)


def _index_covers(indexdef: str, cols: tuple[str, ...] = _INDEX_COLS) -> bool:
    """True when `indexdef` already answers every (tenant_id, collection)
    WHERE deleted_at IS NULL query g's own index would serve.

    pg_indexes.indexdef for a real pg13 btree looks like
        CREATE INDEX ix_vector_search_embeddings_tenant_collection ON
            public.vector_search_embeddings USING btree (tenant_id, collection)
    (a partial one adds ` WHERE (deleted_at IS NULL)`). The key list is the
    first parenthesised group; `asc`/`desc`/`nulls ...` modifiers are
    stripped before comparing columns.

    Column match alone is NOT enough to call an index "covering": a partial
    index on the same two columns but a DIFFERENT predicate (e.g.
    `WHERE tenant_id > 100`) only indexes a slice of the rows and would NOT
    serve g's `deleted_at IS NULL` queries, so it must not be treated as a
    substitute. Two cases count as covering:
      * no WHERE clause at all -> a plain, non-partial index on these
        columns indexes every row, which is a strict superset of the
        `deleted_at IS NULL` subset (this is a's actual live index);
      * a WHERE clause whose predicate text already mentions
        `deleted_at` + `null` -> the same partial index g would create.
    Anything else (3+ key columns, or a WHERE on unrelated columns) is
    NOT covering, so g falls back to creating its own index -- a
    redundant-but-harmless extra index, never a missing one.
    """
    m = re.search(r"\(([^)]*)\)", indexdef or "")
    if not m:
        return False
    keys = [t.strip() for t in m.group(1).split(",")]
    keys = [t for t in keys
            if t and t not in ("asc", "desc")
            and not t.startswith("nulls ")]
    if tuple(keys) != tuple(cols):
        return False
    where_m = re.search(r"\bwhere\b(.*)$", indexdef or "", re.IGNORECASE)
    if not where_m:
        return True  # non-partial: covers every row, including the subset
    predicate = where_m.group(1).lower()
    return "deleted_at" in predicate and "null" in predicate


def _reused_index(indexes: list[tuple[str, str]]) -> str | None:
    """Name of an existing index that already covers (_INDEX_COLS), else None."""
    for name, indexdef in indexes:
        if _index_covers(indexdef):
            return name
    return None


def _sql_statements(indexes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Ordered (label, sql) additive steps; index step honours reuse.

    `indexes` is the live (indexname, indexdef) list from pg_indexes.
    """
    steps = [
        (
            "deleted_by FK -> users (who soft-deleted; no ON DELETE action)",
            f"""
ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS deleted_by INTEGER NULL
    REFERENCES users (id);
""",
        ),
        (
            "updated_at (NOT NULL DEFAULT now(), additive only)",
            f"""
ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL
    DEFAULT now();
""",
        ),
    ]
    if _reused_index(indexes) is None:
        steps.append((
            f"{_INDEX_NAME} partial index on (tenant_id, collection) WHERE live",
            f"""
CREATE INDEX IF NOT EXISTS {_INDEX_NAME}
    ON {_TABLE} ({", ".join(_INDEX_COLS)}) WHERE deleted_at IS NULL;
""",
        ))
    return steps


def _apply(execute, indexes: list[tuple[str, str]]) -> list[str]:
    """Run every step through `execute(sql)`; return the labels applied.

    Kept separate from `main()` so the test can drive it with the
    conftest `db_session` (never the live DB engine).
    """
    applied = []
    for label, sql in _sql_statements(indexes):
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
                   help="Apply the ALTER + index steps. Without this flag, "
                        "dry-run only (no DB connection at all).")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _sql_statements([]):
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print(f"DRY  reuse: skips {_INDEX_NAME} when pg_indexes already has an "
              f"index covering ({', '.join(_INDEX_COLS)}); the reused indexname "
              "is printed at apply time")
        print("DRY  probe: " + _PROBE_TABLE + " (absent table -> "
              "'run 20261011a first', exit 1)")
        print("DRY  downgrade: no-op (append-only per ISO 27001; rows "
              "soft-delete via deleted_at, never a DELETE)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute(_PROBE_TABLE)
        row = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL table probe: {e}")
        return 1
    exists = bool(row) and bool(row[0][0])
    if not exists:
        print(f"FAIL {_TABLE} is not on this DB — run 20261011a first "
              "(g never creates the table, it only extends a's)")
        return 1

    try:
        cur.execute(_PROBE_INDEXES)
        indexes = [(r[0], r[1]) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        print(f"FAIL index probe: {e}")
        return 1
    reused = _reused_index(indexes)
    if reused:
        print(f"SKIP {_INDEX_NAME}: index '{reused}' already covers "
              f"({', '.join(_INDEX_COLS)}) — reused")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    try:
        applied = _apply(cur.execute, indexes)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL apply: {e}")
        return 1
    for label in applied:
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(f"sz-5.4.2g applied: {_TABLE} now carries deleted_by + updated_at "
          f"({len(applied)} statement(s); the rest were already present or "
          "covered by an equivalent index). The lead applies --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
