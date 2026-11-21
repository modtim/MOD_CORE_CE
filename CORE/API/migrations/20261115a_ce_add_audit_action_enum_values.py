"""CE-AUDIT-ENUM-10-VALUES — add 10 AuditAction labels to the `auditaction` enum.

Ten models.AuditAction members were added to the Postgres enum only by the
paid add-on backfill migration (paid_addon/migrations/20261007zzz_auditaction_
enum_backfill.py). An UPGRADED Community Edition database never runs that
add-on chain, so inserts of these audit actions would be rejected with
`invalid input value for enum auditaction` while CE code writes them.

This migration carries each label as a LITERAL statement so the enum-sync
guard (CORE/API/tests/test_audit_action_enum_sync.py) can discover it. The
statements are written as `ALTER TYPE auditaction ADD VALUE IF NOT EXISTS
<label>`, so re-running is a no-op and paid databases (which already have the
labels) are unaffected.

Labels are enum NAMES (uppercase), the form SQLAlchemy stores.
Rollback: none needed; enum labels cannot be removed once added and they are
harmless when unused.

DRY-RUN by default: prints the statements and executes nothing. Run inside
the API container with --commit (the lead applies; this file never does):
    docker exec my_fastapi_app python3 \\
        migrations/20261115a_ce_add_audit_action_enum_values.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


# Must stay LITERAL statements, one per label (see the enum-sync regex).
ALTER_TYPE_STATEMENTS = [
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'SERVICE_ACCOUNT_CREATED'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'SERVICE_ACCOUNT_UPDATED'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'SERVICE_ACCOUNT_DISABLED'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'SERVICE_ACCOUNT_REASSIGNED'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'API_KEY_ADMIN_REVOKED'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'API_KEY_AUTO_DISABLED'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'ASSET_WORKER_ENROLLMENT_ALERT'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'ASSET_WORKER_OUTDATED_ALERT'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'ASSET_WORKER_METADATA_UPDATED'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'API_KEY_ROTATED'",
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
        for stmt in ALTER_TYPE_STATEMENTS:
            print(f"{stmt};")
        print(f"(dry-run; {len(ALTER_TYPE_STATEMENTS)} statements, nothing executed;"
              " pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: ALTER TYPE needs the owner role (CE has no modTech)
    set_role_ok = _owner_role.maybe_set_role(cur)
    added = 0
    for stmt in ALTER_TYPE_STATEMENTS:
        cur.execute(stmt)
        added += 1
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print(f"auditaction enum: {added} value(s) ensured.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
