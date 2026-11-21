"""Generalize permission_requests to cover workflows (not just cogs).

Adds `resource_type` (default 'cog') + `resource_id`, makes `cog_id` nullable,
and backfills resource_id from cog_id for existing rows. This lets the access-
request / review flow handle workflow requests alongside the original cog ones.

Idempotent: ADD COLUMN IF NOT EXISTS / DROP NOT NULL / conditional backfill —
a second run is a no-op.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260623_generalize_permission_requests.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


MIGRATE_SQL = """
ALTER TABLE permission_requests ADD COLUMN IF NOT EXISTS resource_type VARCHAR(20) NOT NULL DEFAULT 'cog';
ALTER TABLE permission_requests ADD COLUMN IF NOT EXISTS resource_id INTEGER;
ALTER TABLE permission_requests ALTER COLUMN cog_id DROP NOT NULL;
UPDATE permission_requests SET resource_id = cog_id WHERE resource_id IS NULL AND cog_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_permission_requests_resource_type ON permission_requests (resource_type);
CREATE INDEX IF NOT EXISTS ix_permission_requests_resource_id ON permission_requests (resource_id);
"""


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
        print(MIGRATE_SQL)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    for stmt in [s.strip() for s in MIGRATE_SQL.split(";") if s.strip()]:
        cur.execute(stmt)
    cur.close()
    print("permission_requests generalized (resource_type/resource_id).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
