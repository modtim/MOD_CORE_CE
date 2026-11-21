"""GY.V2b — export-control US-person flag on worker_licenses.

Adds `us_person_operated` (BOOLEAN, nullable) to `worker_licenses` and its
index (models.py WorkerLicense.us_person_operated, GY.V2b / GSEC R-040).
NULL or FALSE = the worker is NOT certified as US-located/US-person-
operated; the claim guard in routers/worker_tasks.py::_try_claim_task
(withhold EAR/ITAR tasks — and CUI when
compliance.cui_requires_us_person is on — from every worker that is not
flagged TRUE). Set by platform admin via
PATCH /workers/{worker_name}/export-control.

Additive + idempotent: ALTER TABLE ADD COLUMN IF NOT EXISTS /
CREATE INDEX IF NOT EXISTS — a second run is a no-op.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. The lead applies with --commit.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261003d_worker_us_person_operated.py            # dry run: prints SQL
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261003d_worker_us_person_operated.py --commit   # apply

LEAD APPLIES — not applied by the task agent.
"""
from __future__ import annotations

import argparse
import os
import sys

ALTER_SQL = """
ALTER TABLE worker_licenses ADD COLUMN IF NOT EXISTS us_person_operated BOOLEAN;
"""
INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_worker_licenses_us_person_operated
    ON worker_licenses (us_person_operated);
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
    statements = [ALTER_SQL, INDEX_SQL]
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
    cur.execute(f"ALTER TABLE worker_licenses OWNER TO {_owner_role.owner_role(cur)};")
    cur.close()
    print("worker_licenses.us_person_operated ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
