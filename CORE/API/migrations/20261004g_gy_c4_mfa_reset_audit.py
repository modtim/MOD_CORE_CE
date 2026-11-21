"""GY.C4.MFA — add the AUTH_MFA_RESET audit-action enum value.

The admin "Reset 2FA" endpoint (POST /v1/admin/users/{user_id}/mfa/reset,
services/mfa_service.py) writes one security-audit row per reset with
models.AuditAction.AUTH_MFA_RESET. `Enum(AuditAction)` stores the uppercase
Python enum NAME (same foot-gun documented in
20261004e_gy_c4_break_glass_audit.py), so the label added here is the member
NAME. Without this the first reset audit raises DataError.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block — this script
handles that on an autocommit connection (mirrors 20261004b / 20261004e).
Idempotent — re-run is a no-op, and on --commit it re-checks every
models.AuditAction member against the live enum.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261004g_gy_c4_mfa_reset_audit.py          # dry-run
    docker exec my_fastapi_app python3 \\
        migrations/20261004g_gy_c4_mfa_reset_audit.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ENUM_NAME = "auditaction"
SYNC_STATEMENTS = [
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'AUTH_MFA_RESET'",
]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def _existing_values(cur) -> list[str]:
    cur.execute(
        """
        SELECT e.enumlabel
          FROM pg_type t
          JOIN pg_enum e ON t.oid = e.enumtypid
         WHERE t.typname = %s
         ORDER BY e.enumsortorder
        """,
        (ENUM_NAME,),
    )
    return [r[0] for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    if not args.commit:
        for stmt in SYNC_STATEMENTS:
            print(f"{stmt};")
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    current = _existing_values(cur)
    to_add = [s for s in SYNC_STATEMENTS if s.split("'")[1] not in current]
    for stmt in to_add:
        cur.execute(stmt)
    if to_add:
        print(f"Added: {', '.join(s.split(chr(39))[1] for s in to_add)}")
    else:
        print("All values already present — nothing to do.")

    import models  # type: ignore
    after = set(_existing_values(cur))
    cur.close()
    missing = [m.name for m in models.AuditAction if m.name not in after]
    print(f"{ENUM_NAME} now contains {len(after)} values.")
    if missing:
        print(f"DRIFT REMAINS — still missing: {missing}", file=sys.stderr)
        return 1
    print("Sync verified: every models.AuditAction member is in the enum.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
