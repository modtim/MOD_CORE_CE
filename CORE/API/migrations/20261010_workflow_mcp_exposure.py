#!/usr/bin/env python3
"""sz-mcp-per-workflow.1 (mcp-fleet §per-workflow) — the ONE new table:
WORKFLOW MCP EXPOSURE (which workflows are exposed as MCP tools).

revision      : 20261010_workflow_mcp_exposure
down_revision : 20261010b_run_price_quotes

`Workflow.publish_mcp` (models.py, added same commit) is the intent switch
on the workflow; this table is the record of the actual MCP-tool
registration — one row per (workflow, tool_name) exposure, tenant-scoped,
with an `enabled` toggle and created/updated timestamps. Router wiring
(list/expose endpoints) is sz-mcp-per-workflow.2/3, NOT here.

Columns, per the decomp row + models.py WorkflowMcpExposure class:
    id           BIGSERIAL PRIMARY KEY
    workflow_id  INTEGER FK workflows(id) ON DELETE SET NULL, indexed
                 (SET NULL: the exposure row survives its workflow row
                 being pruned — ISO 27001 no-hard-delete posture; the
                 row is the record of what was published)
    tenant_id    INTEGER FK tenants(id) ON DELETE SET NULL, indexed
    tool_name    TEXT NOT NULL
    enabled      BOOLEAN NOT NULL DEFAULT TRUE
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW() (bumped on update)

Indexes: ix_workflow_mcp_exposure_tenant (tenant_id, tool_name) — every
MCP-tools listing is tenant-filtered. Uniqueness: one row per
(workflow_id, tool_name).

Also adds the one column models.py gained on an EXISTING table:
    workflows.publish_mcp  BOOLEAN NOT NULL DEFAULT FALSE
(existing workflows stay unpublished; ALTER only when absent, so re-runs
are no-ops). No other ALTER, no drops.

Idempotent: CREATE TABLE / CREATE INDEX / CREATE UNIQUE INDEX IF NOT EXISTS
+ column-presence guard; safe to re-run. Additive only.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261010_workflow_mcp_exposure.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "workflow_mcp_exposure"

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-mcp-per-workflow.1, MCP-tool exposures)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    workflow_id INTEGER REFERENCES workflows (id) ON DELETE SET NULL,
    tenant_id INTEGER REFERENCES tenants (id) ON DELETE SET NULL,
    tool_name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
    ),
    (
        f"{_TABLE} uniqueness (workflow_id, tool_name)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_tool
    ON {_TABLE} (workflow_id, tool_name);
""",
    ),
    (
        f"{_TABLE} index (tenant_id, tool_name)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant
    ON {_TABLE} (tenant_id, tool_name);
""",
    ),
]


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
                   help="Apply the CREATE TABLE + index steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  would: workflows.publish_mcp column "
              "(ALTER TABLE workflows ADD COLUMN publish_mcp "
              "BOOLEAN NOT NULL DEFAULT FALSE)")
        print("DRY  downgrade: no-op (exposure rows are kept forever; "
              "disable = enabled=false, never a drop)")
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
    if _column_exists(cur, "workflows", "publish_mcp"):
        print("OK   workflows.publish_mcp column already present.")
    else:
        try:
            cur.execute("ALTER TABLE workflows ADD COLUMN "
                        "publish_mcp BOOLEAN NOT NULL DEFAULT FALSE")
            print("OK   workflows.publish_mcp column added.")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL workflows.publish_mcp column: {e}")
            return 1
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "sz-mcp-per-workflow.1 ready: workflow_mcp_exposure exists, "
        "tenant-scoped; routers (sz-mcp-per-workflow.2/3) can list/expose."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
