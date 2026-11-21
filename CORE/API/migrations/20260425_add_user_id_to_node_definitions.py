"""P-wizard A — add user_id column to node_definitions for per-user scoping.

Adds an optional user_id FK so a NodeDefinition can be scoped to:
  - global system  (tenant_id NULL, user_id NULL)
  - tenant-shared  (tenant_id set, user_id NULL)
  - user-private   (tenant_id set, user_id set)

Existing rows: user_id stays NULL → continue to behave as tenant-shared.
Drops the old uq_node_def_tenant_name unique constraint and replaces it
with uq_node_def_tenant_user_name so two users in the same tenant can
each own a private node with the same name without colliding.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260425_add_user_id_to_node_definitions.py --commit
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


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def _constraint_exists(cur, name: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.table_constraints
         WHERE constraint_name = %s
        """,
        (name,),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    steps: list[tuple[str, str]] = []

    if not _column_exists(cur, "node_definitions", "user_id"):
        steps.append((
            "add user_id column",
            "ALTER TABLE node_definitions "
            "ADD COLUMN user_id INTEGER REFERENCES users(id)",
        ))
        steps.append((
            "index user_id",
            "CREATE INDEX IF NOT EXISTS ix_node_definitions_user_id "
            "ON node_definitions(user_id)",
        ))

    if _constraint_exists(cur, "uq_node_def_tenant_name"):
        steps.append((
            "drop old uq_node_def_tenant_name",
            "ALTER TABLE node_definitions "
            "DROP CONSTRAINT IF EXISTS uq_node_def_tenant_name",
        ))
    if not _constraint_exists(cur, "uq_node_def_tenant_user_name"):
        steps.append((
            "add uq_node_def_tenant_user_name",
            "ALTER TABLE node_definitions "
            "ADD CONSTRAINT uq_node_def_tenant_user_name "
            "UNIQUE (tenant_id, user_id, name)",
        ))

    if not steps:
        print("schema already current — nothing to do.")
        return 0

    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        print(f"        {stmt}")
        if args.commit:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
