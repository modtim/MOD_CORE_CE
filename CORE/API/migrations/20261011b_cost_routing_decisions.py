#!/usr/bin/env python3
"""sz-1.3.1 (sz-productivity-floor decomp, §1.3) — the ONE new table:
COST ROUTING DECISIONS (append-only audit of every cost-based variant
routing decision a node made while running).

revision      : 20261011b_cost_routing_decisions
down_revision : 20261011a_vector_search_embeddings

20261011a_vector_search_embeddings is the CURRENT head: no other
migration names it as its down_revision, so this row chains onto it.

This migration only creates storage; no writer/reader code is wired
here — a later decomp row records a decision at variant-selection time
and a later one reads them for routing-feedback / cost-surveillance
sweeps.

Columns, per the sz-1.3.1 decomp row:
    id                  BIGSERIAL PRIMARY KEY
    tenant_id           INTEGER NOT NULL REFERENCES tenants (id)
                        (no ON DELETE action, matching the other audit
                        tables in models.py: a tenant row cannot be
                        hard-deleted out from under its own audit trail)
    workflow_run_id     INTEGER REFERENCES workflow_runs (id)
                        ON DELETE SET NULL (nullable)
    node_id             VARCHAR(128) (nullable) — the in-run node label
    node_definition_id  INTEGER REFERENCES node_definitions (id)
                        ON DELETE SET NULL (nullable)
    chosen_variant      VARCHAR(64) NOT NULL (cuda/rocm/cpu/... or image tag)
    candidates          JSONB NOT NULL DEFAULT '[]' — list of
                        {variant, est_cost_credits, est_latency_s,
                         cache_hit_p, eligible, reason}
    reason              VARCHAR(256) NOT NULL — why this variant won
    budget_cap_credits  NUMERIC(12,2) — applicable workflow_budgets cap
                        at decision time (NULL = no cap)
    forecast_credits    NUMERIC(12,2) — the winning candidate's forecast
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()

ISO 27001 append-only audit: rows are NEVER updated or deleted, so
there is no soft-delete column and no updated_at — the table is pure
history. `workflow_run_id` / `node_definition_id` use ON DELETE SET
NULL (two words): a decision recorded for a since-deleted run or node
definition is still useful routing history, so the row outlives its
parent; `tenant_id` has NO ON DELETE action (matching the other audit
tables in models.py — WorkflowOutputAuditLog, AuditEvent,
PermissionAuditLog, PlatformSettingAudit — none of which cascade on
tenant delete): a tenant row cannot be hard-deleted out from under its
own audit trail.

Indexes: ix_cost_routing_decisions_tenant on (tenant_id),
ix_cost_routing_decisions_run on (workflow_run_id),
ix_cost_routing_decisions_created on (created_at).

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. A matching
models.py ORM class (`CostRoutingDecision`) is added alongside this
migration.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261011b_cost_routing_decisions.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "cost_routing_decisions"

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-1.3.1, append-only variant-routing audit)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    workflow_run_id INTEGER REFERENCES workflow_runs (id) ON DELETE SET NULL,
    node_id VARCHAR(128),
    node_definition_id INTEGER REFERENCES node_definitions (id) ON DELETE SET NULL,
    chosen_variant VARCHAR(64) NOT NULL,
    candidates JSONB NOT NULL DEFAULT '[]',
    reason VARCHAR(256) NOT NULL,
    budget_cap_credits NUMERIC(12,2),
    forecast_credits NUMERIC(12,2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    (
        f"{_TABLE} index (workflow_run_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_run
    ON {_TABLE} (workflow_run_id);
""",
    ),
    (
        f"{_TABLE} index (created_at)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_created
    ON {_TABLE} (created_at);
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
        print("DRY  downgrade: no-op (append-only audit rows are kept as "
              "historical data per ISO 27001; no destructive rollback "
              "defined)")
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
        "sz-1.3.1 ready: cost_routing_decisions exists; a later decomp row "
        "records decisions at variant-selection time."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
