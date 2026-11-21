"""GE.MAA.1 — soft-end marker for assistant sessions.

Adds ONE nullable column:

  assistant_sessions.ended_at
      — timestamptz, set by POST /assistant/session/end. NULL means the
        session is still active. This is a SOFT end (ISO 27001: no hard
        deletes) — the row and its transcript stay queryable after the
        user ends the session. The pre-existing hourly expiry sweep
        (services/assistant_orchestrator.py:expire_sessions) still
        hard-deletes rows once their tenant's retention window elapses;
        this column is orthogonal to that and does not change it.

Purely additive — no existing data touched (the column is NULL until a
session is explicitly ended). Idempotent: ADD COLUMN IF NOT EXISTS;
safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260928_assistant_session_ended_at.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ALTER_ASSISTANT_SESSIONS_SQL = """
ALTER TABLE assistant_sessions
    ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;
"""

STATEMENTS = [
    ALTER_ASSISTANT_SESSIONS_SQL,
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
    print("assistant_sessions.ended_at ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
