"""GSEC FX.152 part 2 (owner decision Q139=B) — rotation-grace audit action +
api_keys grace columns.

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    API_KEY_GRACE_EXPIRED

(API_KEY_ROTATED already exists — added by 20261005j_fx114_service_accounts.py
and reused here for the rotate event.)

api_keys columns (additive, nullable — no defaults needed, existing rows stay
NULL):
    rotated_at     TIMESTAMP WITH TIME ZONE — when this key was rotated
    grace_until    TIMESTAMP WITH TIME ZONE — end of the grace window; the key
                   is soft-disabled (disabled_at stamped) on the first
                   rejection after it

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction block, so
the whole script runs on an autocommit connection (same pattern as
20261007a_fx97_provenance_audit_actions.py). Idempotent: ADD VALUE IF NOT
EXISTS / ADD COLUMN IF NOT EXISTS.

Migration runner is tools/mod_migrate.py (steps discovered as *.py in this
directory, run in filename order, recorded in the append-only release_steps
ledger) — this file sorts after 20261007g.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching the
database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261007h_fx152_rotate_audit.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261007h_fx152_rotate_audit.py --commit   # apply

models.py is bind-mounted live into the API container, so the enum value must
exist in the live `auditaction` type before any rotate writes its audit row —
apply immediately after adding the member to models.py. The two columns are
read through getattr(..., None) in services/api_key_grace.py, so the code
tolerates them being missing; but the rotate endpoint WRITES them, so apply
this migration before the restart that serves the rotate endpoint.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "auditaction.API_KEY_GRACE_EXPIRED value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'API_KEY_GRACE_EXPIRED';
""",
    ),
    (
        "api_keys.rotated_at column",
        """
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS rotated_at TIMESTAMP WITH TIME ZONE;
""",
    ),
    (
        "api_keys.grace_until column",
        """
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS grace_until TIMESTAMP WITH TIME ZONE;
""",
    ),
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
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.152 part 2 rotate audit action + grace columns ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
