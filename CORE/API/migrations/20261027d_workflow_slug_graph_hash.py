#!/usr/bin/env python3
"""2400GX-c9-J1 (30.6.9) -- workflows.slug + workflow_versions.graph_hash.

Additive only, both columns NULLABLE, no default, no backfill (ISO: no writes
to existing rows). `slug` is NULL on every pre-migration workflow -- it is a
per-tenant upsert key for PUT /v1/road/by-slug/{slug}, not a rename.
`graph_hash` is NULL on every pre-migration version; those hash on the fly
from graph_json when a caller needs to compare, and the hash is written only
on NEW versions.

Uniqueness is PER TENANT ONLY, and the index is PARTIAL:
    CREATE UNIQUE INDEX ... ON workflows (tenant_id, slug) WHERE slug IS NOT NULL
There is deliberately NO global UNIQUE on `slug` -- that would contradict T1
(two tenants may each own `demo`) and would leak a cross-tenant oracle. NULL
slugs are excluded from the index, so unslugged rows never collide.

Idempotent: every step is IF NOT EXISTS, so re-running is a no-op.

revision      : 20261027d_workflow_slug_graph_hash
down_revision : 20261027c_node_run_usage_tokens

Class A (same posture as 20261027c_node_run_usage_tokens.py, 20261027b_stack_keep_running.py
and 20261027a_api_keys_deploy_token_stack_id.py): DRY-RUN by default; `--commit`
applies. NEVER applied to the live DB by the lane -- the lead applies it:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261027d_workflow_slug_graph_hash.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "workflows.slug column",
        """
ALTER TABLE workflows
    ADD COLUMN IF NOT EXISTS slug VARCHAR(150);
""",
    ),
    (
        "workflow_versions.graph_hash column",
        """
ALTER TABLE workflow_versions
    ADD COLUMN IF NOT EXISTS graph_hash VARCHAR(64);
""",
    ),
    (
        "uq_workflows_tenant_slug partial unique index",
        """
CREATE UNIQUE INDEX IF NOT EXISTS uq_workflows_tenant_slug
    ON workflows (tenant_id, slug) WHERE slug IS NOT NULL;
""",
    ),
]


_DOWNGRADE = """
DROP INDEX IF EXISTS uq_workflows_tenant_slug;

ALTER TABLE workflows
    DROP COLUMN IF EXISTS slug;

ALTER TABLE workflow_versions
    DROP COLUMN IF EXISTS graph_hash;
"""


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
                   help="Apply the DDL. Without this flag, dry-run only.")
    p.add_argument("--downgrade", action="store_true",
                   help="Drop the columns + index (dry-run unless --commit).")
    args = p.parse_args()

    if args.downgrade:
        if not args.commit:
            print("DRY  would: " + _DOWNGRADE)
            return 0
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(_DOWNGRADE)
        cur.close()
        conn.close()
        print("OK   downgrade: slug/graph_hash + uq_workflows_tenant_slug dropped.")
        return 0

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade available: --downgrade [--commit]")
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
    print("c9-J1 ready: workflows.slug + workflow_versions.graph_hash exist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
