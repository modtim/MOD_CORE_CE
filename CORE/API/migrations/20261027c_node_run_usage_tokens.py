#!/usr/bin/env python3
"""2400GX-c11-J1 (30.6.11) -- node_runs.usage_tokens: per-node token metering.

Additive only: one nullable JSONB column on `node_runs`. NULL is a real state,
not an afterthought -- it means "this node run made no model call", which is
every node run that ever existed and every one that will. That is why there is
no default and no backfill: writing `false`/`{}`/`[]` into 10M rows would turn
"not metered" into "metered, zero", and the metering writers (c11-J2/J4) own
the shape -- `{"models":[{node_id, model, prompt_tokens, completion_tokens,
total_tokens, images}]}`.

Idempotent: the step is IF NOT EXISTS, so re-running is a no-op. No index, no
backfill, no other statement.

revision      : 20261027c_node_run_usage_tokens
down_revision : 20261027b_stack_keep_running

Class A (same posture as 20261027b_stack_keep_running.py and
20261027a_api_keys_deploy_token_stack_id.py): DRY-RUN by default; `--commit`
applies. NEVER applied to the live DB by the lane -- the lead applies it:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261027c_node_run_usage_tokens.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "node_runs.usage_tokens column",
        """
ALTER TABLE node_runs
    ADD COLUMN IF NOT EXISTS usage_tokens JSONB;
""",
    ),
]


_DOWNGRADE = """
ALTER TABLE node_runs
    DROP COLUMN IF EXISTS usage_tokens;
"""


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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the DDL. Without this flag, dry-run only.")
    p.add_argument("--downgrade", action="store_true",
                   help="Drop the column (dry-run unless --commit).")
    args = p.parse_args()

    if args.downgrade:
        if not args.commit:
            print("DRY  would: " + _DOWNGRADE)
            return 0
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(_DOWNGRADE)
        cur.close()
        conn.close()
        print("OK   downgrade: node_runs.usage_tokens dropped.")
        return 0

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade available: --downgrade [--commit]")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        try:
            cur.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print("c11-J1 ready: node_runs.usage_tokens exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
