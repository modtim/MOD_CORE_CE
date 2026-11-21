"""generalized-stack-wake — wake-ownership column.

Adds `persistent_stacks.nim_wake_owned`, the one flag the universal stack-wake
policy (services/stack_wake) needs to know whether IT woke a stack (and may
therefore sleep it back) versus finding it already running (leave-on-if-on, D4).
There is no refcount column and no new task state — how many jobs still need a
stack is counted by querying `api_task_queue` (D4/D5).

Additive + idempotent, defaults FALSE so every existing stack is correctly seen
as "not woken by us" until the policy actually wakes it. A deployment that never
runs this behaves exactly as before (the column simply isn't there — the model
tolerates its absence only in tests, so APPLY THIS before deploying the code).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260918_add_stack_wake_ownership.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


# Defaults FALSE: an upgrade must not make any existing running stack look
# wake-owned (which would let the aggressive sleep-back stop a stack an operator
# stood up). The policy sets it TRUE only when it wakes a stack itself.
STACK_COLUMN_SQL = """
ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS nim_wake_owned BOOLEAN NOT NULL DEFAULT FALSE;
"""

STATEMENTS = [STACK_COLUMN_SQL]


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
    print("stack wake-ownership ready: persistent_stacks.nim_wake_owned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
