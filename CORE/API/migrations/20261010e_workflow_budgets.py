#!/usr/bin/env python3
"""sz-1.2.1 (sz-productivity-floor decomp, §1.2) — the ONE new table:
WORKFLOW BUDGETS (per-workflow max_cost_per_run + per-tenant
max_cost_per_period caps).

revision      : 20261010e_workflow_budgets
down_revision : 20261010d_workflow_parameter_versions

A row with workflow_id NULL is the tenant-wide default budget; a row
with workflow_id set overrides the default for that one workflow. This
migration only creates storage; no enforcement/reader code is wired
here — a later decomp row reads these caps at run-creation / quote time
to reject or warn on over-budget runs.

Columns, per the sz-1.2.1 decomp row:
    id                          BIGSERIAL PRIMARY KEY
    workflow_id                 INTEGER REFERENCES workflows (id)
                                ON DELETE CASCADE (nullable) — NULL means
                                the tenant-wide default row.
    tenant_id                   INTEGER NOT NULL REFERENCES tenants (id)
                                ON DELETE CASCADE
    max_cost_per_run_credits    INTEGER
    max_cost_per_period_credits INTEGER
    period_window               VARCHAR(20) NOT NULL DEFAULT 'monthly'
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()

Unique constraint on (tenant_id, workflow_id): NULLs are distinct in
Postgres, so this allows exactly one tenant-wide default row (NULL
workflow_id) plus one override row per workflow, per tenant.

Indexes: ix_workflow_budgets_tenant on (tenant_id).

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. A matching
models.py ORM class (`WorkflowBudget`) is added alongside this
migration.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261010e_workflow_budgets.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "workflow_budgets"

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-1.2.1, per-workflow/per-tenant cost caps)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    workflow_id INTEGER REFERENCES workflows (id) ON DELETE CASCADE,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    max_cost_per_run_credits INTEGER,
    max_cost_per_period_credits INTEGER,
    period_window VARCHAR(20) NOT NULL DEFAULT 'monthly',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_{_TABLE}_tenant_workflow UNIQUE (tenant_id, workflow_id)
);
""",
    ),
    (
        f"{_TABLE} index (tenant_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant
    ON {_TABLE} (tenant_id);
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
                   help="Apply the CREATE TABLE + index steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (budget rows are kept as "
              "historical/audit data; no destructive rollback defined)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
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
        "sz-1.2.1 ready: workflow_budgets exists; a later decomp row "
        "wires enforcement at run-creation / quote time."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
