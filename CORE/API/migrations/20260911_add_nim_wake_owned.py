"""ephemeral-NIM wake ownership

Adds `persistent_stacks.nim_wake_owned` (BOOLEAN NOT NULL DEFAULT FALSE).

The on-demand local-NIM lifecycle (services/nim_ephemeral.py) must know, at
post-job sleep-back time, whether IT woke the NIM (auto-sleep to free the GPU) or
merely borrowed a NIM that was already on (leave it running — user-requested:
"if it's already on, don't turn it off"). This ownership CANNOT live in
status_reason: that column is worker/reconciler-managed and cleared on every
successful transition, so a marker there does not survive from wake to
sleep-back (the exact bug that left Studio Voice running after a job). This
dedicated column is written ONLY by nim_ephemeral and ignored by every other
subsystem, so it survives intact.

Additive, inert on every existing/non-NIM stack (nim_wake_owned=FALSE →
behaviour unchanged). Idempotent — the startup ensure-schema path in database.py
adds the same column, so this is only needed for out-of-band apply.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260911_add_nim_wake_owned.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ADD_NIM_WAKE_OWNED_SQL = """
ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS nim_wake_owned BOOLEAN NOT NULL DEFAULT FALSE;
"""

STATEMENTS = [ADD_NIM_WAKE_OWNED_SQL]


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
    print("persistent_stacks.nim_wake_owned ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
