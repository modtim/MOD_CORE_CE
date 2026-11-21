"""δ DAG-wizard B4a — build_specs storage.

Persists serialized BuildSpec records keyed by id so the per-task
DAG containers (build_one_variant, smoke_one_variant, etc.) can
fetch the full spec when they run. The DAG nodes carry only a
build_spec_id reference; the heavy artifact bytes (Dockerfiles,
sample input, etc.) stay in this table.

Schema:
- id (PK)
- tenant_id (FK, indexed) — tenant who initiated the build
- workflow_run_id (FK, indexed, nullable) — the build-DAG run
  consuming this spec; nullable because we create the spec BEFORE
  the workflow run and link them together once both exist
- spec_json (JSONB) — the full BuildSpec payload
- created_at (timestamptz)

Lifecycle:
- Wizard's /build-and-register-node endpoint INSERTs a row, gets
  back an id, embeds it in the graph_json's node data so each
  per-task container can fetch the spec when it runs.
- Old rows are pruned by retention_cleanup (system_tasks) the
  same way old workflow_run logs get cleaned up.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260507_add_build_specs_table.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    steps: list[tuple[str, str]] = []

    if not _table_exists(cur, "build_specs"):
        steps.append((
            "create build_specs table",
            """
            CREATE TABLE build_specs (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                workflow_run_id INTEGER REFERENCES workflow_runs(id) ON DELETE SET NULL,
                spec_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
        ))
        steps.append((
            "index tenant_id",
            "CREATE INDEX IF NOT EXISTS ix_build_specs_tenant ON build_specs(tenant_id)",
        ))
        steps.append((
            "index workflow_run_id",
            "CREATE INDEX IF NOT EXISTS ix_build_specs_workflow_run ON build_specs(workflow_run_id)",
        ))
        steps.append((
            "index created_at for retention cleanup",
            "CREATE INDEX IF NOT EXISTS ix_build_specs_created_at ON build_specs(created_at)",
        ))

    if not steps:
        print("schema already current — nothing to do.")
        return 0

    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        print(f"        {stmt.strip()[:200]}")
        if args.commit:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
