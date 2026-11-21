"""manager external (LB-fronted) URL — distinct from the peer/internal URL

A manager behind a load balancer has TWO addresses:
  - url           — the PEER/internal address other managers use (gossip/handoff),
                    bypassing the client LB (an IP / private DNS on real VMs).
  - external_url  — the PUBLIC, LB-fronted address clients use for this region
                    (e.g. https://example.com), for display + entry.

This adds external_url to managers + manager_enrollment_tokens. Additive + idempotent.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260616_add_manager_external_url.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


SQL = """
ALTER TABLE managers
    ADD COLUMN IF NOT EXISTS external_url VARCHAR(512);
ALTER TABLE manager_enrollment_tokens
    ADD COLUMN IF NOT EXISTS external_url VARCHAR(512);
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
    print("managers.external_url + manager_enrollment_tokens.external_url ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
