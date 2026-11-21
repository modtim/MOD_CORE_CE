"""worker enrollment home anchor — local (site) vs remote (cloud)

dual-anchor-mesh-federation §6.2. Records, per enrollment token, which control
plane a worker homes to so the preauth key is minted against the right Headscale.
Additive + idempotent; backfills existing rows to 'local'.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260904_add_worker_enrollment_home_anchor.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

SQL = """
ALTER TABLE worker_enrollment_tokens
    ADD COLUMN IF NOT EXISTS home_anchor VARCHAR(16) NOT NULL DEFAULT 'local';
"""

STATEMENTS = [SQL]


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
    print("worker_enrollment_tokens.home_anchor ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
