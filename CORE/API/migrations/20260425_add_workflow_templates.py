"""P-wizard B — workflow_templates table.

Starter workflow scaffolds users can clone into their workspace. Same
JSON graph shape as WorkflowVersion.graph_json so a clone is just a
straight copy + a new Workflow + WorkflowVersion pair.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260425_add_workflow_templates.py --commit
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


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (name,),
    )
    return cur.fetchone() is not None


_CREATE = """
CREATE TABLE workflow_templates (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER REFERENCES tenants(id),
    name            VARCHAR(160) NOT NULL,
    description     TEXT,
    category        VARCHAR(80),
    graph_json      JSONB NOT NULL,
    preview_nodes   JSONB,
    plan_required   VARCHAR(40),
    is_curated      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_workflow_template_tenant_name UNIQUE (tenant_id, name)
);
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_workflow_templates_tenant_id ON workflow_templates(tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_workflow_templates_category ON workflow_templates(category)",
    "CREATE INDEX IF NOT EXISTS ix_wfx_template_curated ON workflow_templates(is_curated)",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if _table_exists(cur, "workflow_templates"):
        print("workflow_templates already exists — checking indexes only.")
        if args.commit:
            for stmt in _INDEXES:
                cur.execute(stmt)
        return 0

    print(("WRITE" if args.commit else "DRY  "), "create workflow_templates")
    if args.commit:
        cur.execute(_CREATE)
        for stmt in _INDEXES:
            cur.execute(stmt)
        print("Applied.")
    else:
        print("(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
