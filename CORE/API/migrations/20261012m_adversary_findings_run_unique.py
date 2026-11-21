#!/usr/bin/env python3
"""496 / wsip-6.6-hardening -- one adversary finding per workflow RUN.

services/adversary_outcome.py checks "does a finding already name this
workflow_run_id?" and then calls persist_finding. Two collectors running at
once (the collect sweep + a manual dispatch) can BOTH pass that check and
BOTH insert: adversary_findings (migration 20261012e_adversary_tables.py) has
no unique index on workflow_run_id, so the DB happily keeps the duplicate.

This migration adds the missing constraint:

    CREATE UNIQUE INDEX IF NOT EXISTS ux_adversary_findings_workflow_run
        ON adversary_findings (workflow_run_id)
        WHERE workflow_run_id IS NOT NULL AND deleted_at IS NULL

PARTIAL on the live rows only (ISO 27001): a soft-deleted finding is a
tombstone and must not block a fresh finding for the same run, and rows whose
run id is NULL (findings not tied to one dispatch) are unconstrained -- the
WHERE clause excludes them, so they can repeat freely.

GUARD FIRST, REPORT-ONLY: before the CREATE, a SELECT lists every
workflow_run_id that ALREADY has >1 live finding. It deletes and modifies
nothing -- ISO 27001 forbids hard deletes, so the operator resolves a
duplicate group by soft-deleting (deleted_at = NOW()) the rows they want
tombstoned, leaving one live. If any group exists the migration ABORTS
(exit 1) without touching the schema, because a UNIQUE index cannot be built
over existing duplicates and silently picking a winner would be a data
decision this migration must not make.

revision      : 20261012m_adversary_findings_run_unique
down_revision : 20261012l_taskstatus_suspended_awaiting

Idempotent: CREATE UNIQUE INDEX IF NOT EXISTS -- a re-run with the index
present is a clean no-op. Additive only: no ALTER, no drops, no DELETE.
DRY-RUN by default; --commit applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012m_adversary_findings_run_unique.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_AF_TABLE = "adversary_findings"
_INDEX_NAME = "ux_adversary_findings_workflow_run"

_GUARD_SQL = f"""
SELECT workflow_run_id AS run_id,
       COUNT(*)       AS live_rows,
       MIN(id)        AS first_id
  FROM {_AF_TABLE}
 WHERE workflow_run_id IS NOT NULL AND deleted_at IS NULL
 GROUP BY workflow_run_id
 HAVING (COUNT(*) > 1)
 ORDER BY workflow_run_id;
"""

_STATEMENTS = [
    (
        f"GUARD {_AF_TABLE}: report live duplicate groups per "
        "workflow_run_id (read-only; aborts if any)",
        _GUARD_SQL,
    ),
    (
        f"{_AF_TABLE} partial UNIQUE {_INDEX_NAME} (workflow_run_id) "
        "WHERE live",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME}
    ON {_AF_TABLE} (workflow_run_id)
    WHERE workflow_run_id IS NOT NULL AND deleted_at IS NULL;
""",
    ),
]


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
                   help="Apply the UNIQUE index. Without this flag, "
                        "dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (the index is additive; rollback keeps "
              "it -- no row depends on it, and dropping it would only widen "
              "the duplicate window the writer already guards)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)

    label, sql = _STATEMENTS[0]
    try:
        cur.execute(sql)
        dupes = cur.fetchall()
    except Exception as e:  # noqa: BLE001 -- table absent on a fresh schema
        print(f"SKIP {label}: {e} "
              f"({_AF_TABLE} not found -- create_all builds the tables from "
              "the migration-e DDL first; nothing to constrain yet)")
        return 0
    if dupes:
        print(f"ABORT {label}: {len(dupes)} workflow_run_id(s) already carry "
              ">1 live finding:")
        for run_id, live_rows, first_id in dupes[:20]:
            print(f"  run {run_id}: {live_rows} live rows "
                  f"(oldest id {first_id})")
        print("A UNIQUE index cannot be built over existing duplicates and "
              "this migration will not pick a winner. Resolve them YOURSELF "
              "(ISO 27001: soft-delete, never DELETE) -- stamp deleted_at on "
              "every row but the one you keep -- then re-run.")
        return 1
    print(f"OK   {label}: no live duplicate groups")

    label, sql = _STATEMENTS[1]
    try:
        cur.execute(sql)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {label}: {e}")
        return 1
    print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print("wsip6.6 ready: at most ONE live adversary_findings row per "
          "workflow_run_id; persist_finding's ON CONFLICT DO NOTHING now has "
          "a constraint to fire on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
