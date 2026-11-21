"""workflow home region (add-multi-region-mesh — triggers <-> mesh seam)

Adds `workflows.home_zone` so a workflow carries the region it belongs to. A
trigger that fires in another region for a workflow whose home zone is owned by
an approved peer manager is RELAYED to that manager (which runs it with its own
data), instead of always running locally and region-blind.

Additive and inert on a single-manager / unset-region deployment (home_zone stays
NULL → runs locally, exactly as before). Idempotent.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260616_add_workflow_home_zone.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ADD_WORKFLOW_HOME_ZONE_SQL = """
ALTER TABLE workflows
    ADD COLUMN IF NOT EXISTS home_zone VARCHAR(100);
CREATE INDEX IF NOT EXISTS ix_workflows_home_zone ON workflows (home_zone);
"""

STATEMENTS = [ADD_WORKFLOW_HOME_ZONE_SQL]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        for s in STATEMENTS:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    for s in STATEMENTS:
        cur.execute(s)
    cur.close()
    print("workflows.home_zone ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
