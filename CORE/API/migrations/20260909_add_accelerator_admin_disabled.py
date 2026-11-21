"""accelerator quarantine — operator disable flag

Adds `accelerator_profiles.admin_disabled` (BOOLEAN NOT NULL DEFAULT FALSE).

An operator one-click "disable this accelerator" flag, orthogonal to
benchmark_status (which records the last benchmark OUTCOME). A disabled device
is excluded from scheduling exactly like a failed/unsupported one, WITHOUT
re-benchmarking or destroying its scores; re-enabling clears the flag.

Part of graceful accelerator degradation: "a broken GPU can't keep the system
from going." Additive, inert on existing rows (admin_disabled=FALSE → device
behaves exactly as before). Idempotent — the startup ensure-schema path in
database.py adds the same column, so this is only needed for out-of-band apply.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260909_add_accelerator_admin_disabled.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ADD_ADMIN_DISABLED_SQL = """
ALTER TABLE accelerator_profiles
    ADD COLUMN IF NOT EXISTS admin_disabled BOOLEAN NOT NULL DEFAULT FALSE;
"""

STATEMENTS = [ADD_ADMIN_DISABLED_SQL]


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
    print("accelerator_profiles.admin_disabled ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
