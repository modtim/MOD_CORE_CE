#!/usr/bin/env python3
"""2400GX-c3-J1 (30.6.3) -- api_keys += deploy_token_stack_id: the per-stack
DEPLOY credential pin. A row with it set IS a deploy token (its scopes live in
the existing capability_scope JSONB as ["stack:deploy", "stack:logs", ...]);
NULL = an ordinary key, behaviour unchanged. Additive only: no drops, no
renames, no backfill. Idempotent: every step is IF NOT EXISTS, so re-running
is a no-op.

revision      : 20261027a_api_keys_deploy_token_stack_id
down_revision : 20261026a_workflow_run_idempotency_keys

Class A (same posture as 20261026a_workflow_run_idempotency_keys.py and
20261007h_fx152_rotate_audit.py): DRY-RUN by default; `--commit` applies.
NEVER applied to the live DB by the lane -- the lead applies it:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261027a_api_keys_deploy_token_stack_id.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "api_keys.deploy_token_stack_id column (FK -> persistent_stacks.id)",
        """
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS deploy_token_stack_id INTEGER
        REFERENCES persistent_stacks(id);
""",
    ),
    (
        "pin index ix_api_keys_deploy_token_stack_id",
        """
CREATE INDEX IF NOT EXISTS ix_api_keys_deploy_token_stack_id
    ON api_keys (deploy_token_stack_id);
""",
    ),
]


_DOWNGRADE = """
ALTER TABLE api_keys
    DROP COLUMN IF EXISTS deploy_token_stack_id;
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
        print("OK   downgrade: api_keys.deploy_token_stack_id dropped.")
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
    print("c3-J1 ready: api_keys.deploy_token_stack_id exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
