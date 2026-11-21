#!/usr/bin/env python3
"""wep1.a (workflow-expressiveness-primitives phase 1) — declared workflow
parameters column.

revision      : 20261011j_workflow_parameters_column
down_revision : 20261011i_workflow_budgets_default_unique

Adds the one column models.py gained on an EXISTING table:
    workflows.parameters  JSONB NULL
(the workflow-level parameter declarations; NULL = no parameters declared,
so existing workflows are unchanged). ALTER only when absent, so re-runs are
no-ops. No other ALTER, no drops, no backfill.

DRY-RUN by default; `--commit` applies. Lead-applied.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261011j_workflow_parameters_column.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_ALTER = "ALTER TABLE workflows ADD COLUMN parameters JSONB"


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


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
                   help="Apply the ALTER. Without this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        print(f"DRY  would: workflows.parameters column ({_ALTER})")
        print("DRY  downgrade: no-op (column kept; additive only)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    if _column_exists(cur, "workflows", "parameters"):
        print("OK   workflows.parameters column already present.")
    else:
        try:
            cur.execute(_ALTER)
            print("OK   workflows.parameters column added.")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL workflows.parameters column: {e}")
            return 1
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print("wep1.a ready: workflows.parameters exists (NULL = none declared).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
