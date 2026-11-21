#!/usr/bin/env python3
"""sz-1.9.1 (sz-productivity-floor decomp §1.9, owner Q-SZ1.9 = A) — the
ONE new table: WORKFLOW RUN DISPATCH PRIORITY (the cost/capacity dispatch
tier a run opted into, kept in a NEW 1:1 side table, NOT a new column on
workflow_runs).

revision      : 20261011c_workflow_run_dispatch_priority
down_revision : 20261011b_cost_routing_decisions

20261011b_cost_routing_decisions is the CURRENT head: no other migration
names it as its down_revision (verified by grep over migrations/), so this
row chains onto it.

Why a side table (Q-SZ1.9 = A): workflow_runs is the hottest table in the
platform (every node/task/audit row FKs it); ALTERing a column onto it
means touching a huge table at deploy time. A 1:1 side table is created
cheaply and is read only by the dispatcher.

This is NOT workflow_runs.priority_class (the packer's scheduling class:
normal/high/low/interactive). dispatch_priority is the COST/CAPACITY tier:
'on_demand' (default: full-price worker, guaranteed capacity) or 'spot'
(the 5-10x cheaper spot / preemptible pool per workflow-cost-economics;
spot runs are killed and re-queued when capacity is reclaimed). A run with
NO row here means 'on_demand' — absence is not null.

Columns, per the sz-1.9.1 decomp row:
    id              BIGSERIAL PRIMARY KEY
    workflow_run_id INTEGER NOT NULL REFERENCES workflow_runs (id), UNIQUE
                    — NO ON DELETE action (matching the other 1:1 run
                    side tables in models.py: RunPriceQuote,
                    WorkflowMcpExposure, RunProofBundle — none of which
                    cascade; soft-delete platform, ISO 27001 no hard
                    deletes)
    tenant_id       INTEGER NOT NULL REFERENCES tenants (id)
                    — no ON DELETE action either: a tenant row cannot be
                    hard-deleted out from under its own dispatch record
    dispatch_priority VARCHAR(16) NOT NULL DEFAULT 'on_demand'
                    CHECK(dispatch_priority IN ('on_demand', 'spot'))
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    (the tier is mutable — an admin can re-dispatch a
                    stuck run onto spot — so updated_at exists, unlike the
                    append-only audit tables)

Indexes: ix_workflow_run_dispatch_priority_tenant on (tenant_id).
workflow_run_id already carries UNIQUE (its own index).

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.
ON DELETE SET NULL is two words where used at all — not used here.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. A matching models.py
ORM class (`WorkflowRunDispatchPriority`) is added alongside this
migration.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261011c_workflow_run_dispatch_priority.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "workflow_run_dispatch_priority"
_REVISION = "20261011c_workflow_run_dispatch_priority"
_DOWN_REVISION = "20261011b_cost_routing_decisions"
_ALLOWED_PRIORITIES = ("on_demand", "spot")

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-1.9.1, 1:1 run dispatch-tier side table)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    workflow_run_id INTEGER NOT NULL REFERENCES workflow_runs (id),
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    dispatch_priority VARCHAR(16) NOT NULL DEFAULT 'on_demand'
        CHECK (dispatch_priority IN ('on_demand', 'spot')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
    ),
    (
        f"{_TABLE} UNIQUE constraint on workflow_run_id (1:1 with runs)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_run
    ON {_TABLE} (workflow_run_id);
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
                   help="Apply the CREATE TABLE + unique + index steps. "
                        "Without this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (the tier record is dispatch history; "
              "kept per ISO 27001 — no destructive rollback defined)")
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
        "sz-1.9.1 ready: workflow_run_dispatch_priority exists; a run with "
        "no row dispatches on_demand. The lead applies --commit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
