"""resource lock: reason note + renewed_at + longer default TTL support

Adds two additive columns to `resource_locks`:
  - `reason`      TEXT   — optional free-text note the holder attaches
                           ("editing hero shot"), surfaced in the lock UI.
  - `renewed_at`  TIMESTAMPTZ — last time the holder extended the lock.

Both are nullable and inert on existing rows. This pairs with the code change
that (a) makes the lock TTL configurable via RESOURCE_LOCK_TTL_MINUTES
(default 480 = 8h, replacing the surprise 15-minute expiry) and (b) adds
renew + admin force-release endpoints. Additive + idempotent.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260907_resource_lock_reason_renewed.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ADD_LOCK_COLUMNS_SQL = """
ALTER TABLE resource_locks
    ADD COLUMN IF NOT EXISTS reason TEXT,
    ADD COLUMN IF NOT EXISTS renewed_at TIMESTAMPTZ;
"""

STATEMENTS = [ADD_LOCK_COLUMNS_SQL]


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
    print("resource_locks.reason + renewed_at ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
