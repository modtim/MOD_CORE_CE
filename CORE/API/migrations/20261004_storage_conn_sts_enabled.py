"""GSEC.Q21.1 — per-storage-connection STS opt-in flag.

Adds `sts_enabled` (BOOLEAN, NOT NULL, server default FALSE) to
`storage_connections` (models.py StorageConnection.sts_enabled).

When a tenant's BYO-S3 connection opts in (default FALSE = today's
behaviour), the API mints short-lived STS AssumeRole session creds scoped
to the tenant's bucket + the run's prefix for task containers instead of
handing out the tenant's static key (GSEC.Q21, design 3.1). Minting is
still fail-open: if the tenant's endpoint doesn't actually support STS
the API falls back to the static key with a warning.

Additive + idempotent: ALTER TABLE ADD COLUMN IF NOT EXISTS — a second
run is a no-op.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261004_storage_conn_sts_enabled.py            # dry run: prints SQL
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261004_storage_conn_sts_enabled.py --commit   # apply
"""
from __future__ import annotations

import argparse
import os
import sys

ALTER_SQL = """
ALTER TABLE storage_connections
    ADD COLUMN IF NOT EXISTS sts_enabled BOOLEAN NOT NULL DEFAULT false;
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
    statements = [ALTER_SQL]
    if not args.commit:
        for s in statements:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    for s in statements:
        cur.execute(s)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — re-own to the current user
    cur.execute(f"ALTER TABLE storage_connections OWNER TO {_owner_role.owner_role(cur)};")
    cur.close()
    print("storage_connections.sts_enabled ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
