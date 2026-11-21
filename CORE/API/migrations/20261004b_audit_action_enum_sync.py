"""GSEC Q14 follow-on — sync the `auditaction` Postgres enum with models.AuditAction.

A live drift check (`enumlabel` of `auditaction` vs the Python enum, 2026-10-04)
found three Python members with no Postgres label — inserting any of them
(PermissionAuditLog.action, `Enum(AuditAction)`, which stores the enum NAME)
fails with:

    sqlalchemy.exc.DataError: invalid input value for enum
    auditaction: "VOLUME_DELETE_GUARD"

The most visible victim is scripts/record_volume_guard.py (GSEC Q14,
commit 495d48330): every volume-guard audit row it writes raises and the
attempt never reaches the security audit log.

Convention copied from the existing ALTER TYPE migrations (20260425_add_
content_rename_audit_actions, 20260605_add_sso_audit_action_enum_values):
one literal `ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '<NAME>'` per
value, each executed on an autocommit connection because ADD VALUE cannot
run inside a transaction block. Idempotent — re-run is a no-op.

On --commit the script also re-runs the full drift check against
models.AuditAction and exits non-zero if any member is still missing, so
this migration doubles as the sync guard.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261004b_audit_action_enum_sync.py          # dry-run
    docker exec my_fastapi_app python3 \\
        migrations/20261004b_audit_action_enum_sync.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ENUM_NAME = "auditaction"
# One literal statement per missing member — Python enum NAMES (uppercase),
# the stored form in this column, matching every other auditaction migration.
SYNC_STATEMENTS = [
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'PLATFORM_UPDATE_APPLIED'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'VOLUME_DELETE_GUARD'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'ADMIN_HARBOR_STORAGE_QUOTA_OVERRIDE'",
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

    # Full sync guard: no models.AuditAction member may be missing now.
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
