#!/usr/bin/env python3
"""task 240 — PARTIAL UNIQUE INDEX: one tenant-default budget row per tenant.

revision      : 20261011i_workflow_budgets_default_unique
down_revision : 20261011h_workflow_breakpoints

20261011h_workflow_breakpoints is the CURRENT head: no other migration
names it as its down_revision (verified by grep over migrations/), so this
row chains onto it.

Why: workflow_budgets carries UniqueConstraint(tenant_id, workflow_id)
from 20261010e_workflow_budgets. Postgres treats NULLs as distinct in
plain UNIQUE constraints, so nothing stopped a tenant from accumulating
MANY tenant-default rows (workflow_id IS NULL). A row with workflow_id
NULL is the tenant-wide default budget (see 20261010e docstring); there
must be exactly ONE such row per tenant, while override rows (workflow_id
set) keep the existing per-(tenant, workflow) uniqueness.

The PARTIAL UNIQUE uq_workflow_budgets_tenant_default on
workflow_budgets (tenant_id) WHERE workflow_id IS NULL makes that a
database fact: a second tenant-default row for the same tenant is a hard
IntegrityError, while override rows (workflow_id NOT NULL) are outside
the index and unaffected.

Pre-check (ISO 27001: no hard deletes): before creating the index, count
workflow_id IS NULL rows per tenant. If any tenant already has 2+, the
migration PRINTS the offending tenant_ids + counts and exits non-zero
WITHOUT creating the index and WITHOUT touching a single row — dedupe
policy (which row survives) is the lead's decision, not this migration's.

Idempotent: CREATE UNIQUE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables, no new table.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \
        migrations/20261011i_workflow_budgets_default_unique.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "workflow_budgets"
_INDEX = "uq_workflow_budgets_tenant_default"
_REVISION = "20261011i_workflow_budgets_default_unique"
_DOWN_REVISION = "20261011h_workflow_breakpoints"

_PRECHECK_SQL = f"""
SELECT tenant_id, COUNT(*)
    FROM {_TABLE}
    WHERE workflow_id IS NULL
    GROUP BY tenant_id
    HAVING COUNT(*) > 1;
"""

_STATEMENTS = [
    (
        f"{_TABLE} partial UNIQUE (tenant_id) WHERE tenant-default "
        "(workflow_id IS NULL)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX}
    ON {_TABLE} (tenant_id)
    WHERE workflow_id IS NULL;
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
                   help="Apply the CREATE UNIQUE INDEX step. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  precheck: would count workflow_id IS NULL rows per "
              "tenant and refuse (exit 1, rows untouched) if any tenant "
              "has 2+; no rows are ever deleted here")
        print("DRY  downgrade: no-op (budget rows are kept as "
              "historical/audit data; no destructive rollback defined)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)

    try:
        cur.execute(_PRECHECK_SQL)
        dupes = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL precheck (tenant-default duplicates): {e}")
        return 1
    if dupes:
        print("REFUSE: tenants already hold 2+ tenant-default "
              "(workflow_id IS NULL) budget rows; the UNIQUE index "
              "cannot be created. Rows are NOT touched (ISO 27001: "
              "no hard deletes) — the lead decides which row survives:")
        for tenant_id, count in dupes:
            print(f"  tenant_id={tenant_id} tenant-default rows={count}")
        if set_role_ok:
            cur.execute("RESET ROLE")
        cur.close()
        conn.close()
        return 1

    for label, sql in _STATEMENTS:
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
    print(
        "task 240 ready: workflow_budgets now allows at most ONE "
        "tenant-default (workflow_id IS NULL) row per tenant; "
        "override rows unchanged. The lead applies --commit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
