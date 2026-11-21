"""GY.C4 — add the AUTH_BREAK_GLASS / AUTH_BREAK_GLASS_REVIEWED audit-action
enum values.

The break-glass 2FA reset (CORE/startup/break-glass.sh) records each
emergency MFA reset as one security-audit row (`audit.log_security_event`
with models.AuditAction.AUTH_BREAK_GLASS), and the follow-up review
(POST /v1/admin/break-glass/{audit_id}/review) writes AUTH_BREAK_GLASS_
REVIEWED. `Enum(AuditAction)` stores the uppercase Python enum NAME
(same foot-gun as apitaskstatus — see database._migrate_schema_impl
NOTE), so the labels added here are the member NAMES.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block — this
script handles that on an autocommit connection (mirrors
20261004b_audit_action_enum_sync.py). Idempotent — re-run is a no-op.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261004e_gy_c4_break_glass_audit.py          # dry-run
    docker exec my_fastapi_app python3 \\
        migrations/20261004e_gy_c4_break_glass_audit.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ENUM_NAME = "auditaction"
SYNC_STATEMENTS = [
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'AUTH_BREAK_GLASS'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'AUTH_BREAK_GLASS_REVIEWED'",
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
        cur.execute(stmt)
    if to_add:
        print(f"Added: {', '.join(s.split(chr(39))[1] for s in to_add)}")
    else:
        print("All values already present — nothing to do.")

    # Sync guard: no models.AuditAction member may be missing now.
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
