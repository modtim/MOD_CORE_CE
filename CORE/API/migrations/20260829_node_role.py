"""node-role §1 — authoritative user/system classification for nodes.

Adds `node_definitions.node_role` (VARCHAR(16), NOT NULL, default 'system'):
  - 'user'   → visible in the WorkflowBuilder palette + composed by the wizard.
  - 'system' → hidden from the palette + wizard, but the orchestrator can still
               insert and run it (auto-converters, benchmarks, etc.).

`node_role` REPLACES `system_only` as the source of truth; `system_only` is kept
as a derived alias (synced by a model event) so the 20+ existing filters keep
working. Backfill preserves current classification:
  node_role = 'system' where system_only is TRUE, else 'user'.

Conservative default ('system') so a newly-seeded internal node stays hidden
until a developer promotes it.

Run inside the API container:

    docker exec my_fastapi_app python3 migrations/20260829_node_role.py --commit

Idempotent — existence is tested before the ALTER.
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
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (table,))
    return cur.fetchone() is not None


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if not _table_exists(cur, "node_definitions"):
        print("Table node_definitions absent — refusing to run.")
        return 1

    if _column_exists(cur, "node_definitions", "node_role"):
        print("Column node_definitions.node_role already present.")
    elif not args.commit:
        print("DRY RUN: would add node_definitions.node_role VARCHAR(16) NOT NULL "
              "DEFAULT 'system' and backfill from system_only")
        print("\nDry run only. Re-run with --commit to apply.")
        return 0
    else:
        cur.execute(
            "ALTER TABLE node_definitions ADD COLUMN node_role VARCHAR(16) "
            "NOT NULL DEFAULT 'system'"
        )
        print("Added node_definitions.node_role.")

    if args.commit:
        # Backfill: preserve current classification. system_only=TRUE → system.
        cur.execute(
            "UPDATE node_definitions "
            "SET node_role = CASE WHEN system_only THEN 'system' ELSE 'user' END"
        )
        print("Backfilled node_role from system_only.")

    if not args.commit:
        print("\nDry run only. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
