"""launch-completion-megaplan GC.PFM.2 — tenant calibration-fixture migration state.

Adds three nullable columns to `tenants` (purely additive; no existing
table/data touched):

  fixture_status     VARCHAR(16) DEFAULT 'pending'
                     pending -> ready|failed via the migrator callback;
                     the daily drift tick flips ready -> drift (alert-only,
                     no auto-repair — PFM.1 owner decision).
  fixture_version    VARCHAR(32)  manifest version of the last successful
                                  shared-bucket upload (platform-calibration-data).
  fixture_checked_at TIMESTAMPTZ  when the migrator/audit last verified this
                                  tenant's fixtures.

Idempotent: `ADD COLUMN IF NOT EXISTS`; safe to re-run. ISO 27001:
additive only, no deletes.

Run inside the API container (dry-run by default):
    docker exec my_fastapi_app python3 \\
        migrations/20260925_add_tenant_fixture_status.py
    docker exec my_fastapi_app python3 \\
        migrations/20260925_add_tenant_fixture_status.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


STATEMENTS = [
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS fixture_status VARCHAR(16) DEFAULT 'pending';",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS fixture_version VARCHAR(32);",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS fixture_checked_at TIMESTAMPTZ;",
]

COLUMNS = ["fixture_status", "fixture_version", "fixture_checked_at"]


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
    p = argparse.ArgumentParser(description=__doc__)
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
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='tenants' AND column_name IN "
        "('fixture_status','fixture_version','fixture_checked_at') "
        "ORDER BY column_name"
    )
    present = sorted(row[0] for row in cur.fetchall())
    cur.close()
    if present != sorted(COLUMNS):
        print(f"ERROR: expected columns {sorted(COLUMNS)}, found {present}")
        return 1
    print("tenants fixture columns ready: fixture_status, fixture_version, fixture_checked_at")
    return 0


if __name__ == "__main__":
    sys.exit(main())
