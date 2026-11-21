"""GA.ASW.4 — stack health grade: per-service compose state on observations.

Adds ONE nullable column:

  stack_observations.service_states
      — JSONB, list of {"name", "state", "health"} dicts the worker reports
        per compose service when it has that detail to give. NULL means "not
        reported this beat" (older worker binary, or nothing to report);
        `services.stack_observation.stack_health_grade` falls back to the
        existing running/total counts when it is NULL.

Purely additive — no existing data touched (the column is NULL until a
worker reports per-service state). Idempotent: ADD COLUMN IF NOT EXISTS;
safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260928_stack_observation_service_states.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ALTER_STACK_OBSERVATIONS_SQL = """
ALTER TABLE stack_observations
    ADD COLUMN IF NOT EXISTS service_states JSONB;
"""

STATEMENTS = [
    ALTER_STACK_OBSERVATIONS_SQL,
]


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
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for s in STATEMENTS:
        cur.execute(s)
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("stack_observations.service_states ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
