"""GY.C1 — add the COMPLIANCE_PROFILE_LOCK_REJECTED audit-action enum value.

A settings write that weakens a value an active compliance profile
requires is refused (HTTP 409) and recorded as one security-audit row
(`audit.log_admin_action` with
models.AuditAction.COMPLIANCE_PROFILE_LOCK_REJECTED; metadata carries
the setting key, attempted value, blocking profile and requirement).
`Enum(AuditAction)` stores the uppercase Python enum NAME (same foot-gun
as the 20261004b drift fix), so the label added here is the member NAME.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block — this
script handles that on an autocommit connection (mirrors
20261004b_audit_action_enum_sync.py). Idempotent — re-run is a no-op.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261005b_gy_c1_profile_lock_audit.py          # dry-run
    docker exec my_fastapi_app python3 \\
        migrations/20261005b_gy_c1_profile_lock_audit.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ENUM_NAME = "auditaction"
SYNC_STATEMENTS = [
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'COMPLIANCE_PROFILE_LOCK_REJECTED'",
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
    to_add = [s for s in SYNC_STATEMENTS
              if s.split("'")[1] not in current]
    for stmt in to_add:
        # ADD VALUE IF NOT EXISTS (PG >= 12); each statement runs on this
        # autocommit connection — never inside a transaction block.
        cur.execute(stmt)
    if to_add:
        print(f"Added: {', '.join(s.split(chr(39))[1] for s in to_add)}")
    else:
        print("All values already present — nothing to do.")
    cur.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
