"""FX.101 part 1 (owner decision Q87=A) — tenant IP allowlist: enum values.

Per-tenant storage rides the EXISTING tenant-scoped platform-settings
mechanism (GS.PSR.2): `auth.tenant_ip_allowlist` (list of CIDR strings,
written per tenant with scope='tenant' by
PUT /v1/tenant/security/ip-allowlist) plus the global kill switch
`auth.ip_allowlist_enabled` (bool, default true). NO tenants column is
added — a tenant-scoped settings row is the tenant setting.

This migration only adds the two `AuditAction` enum members backing
the audit rows, because `PermissionAuditLog.action` is a native
Postgres enum (`auditaction`) that stores the enum NAME:

    TENANT_IP_BLOCKED          — a request was refused (403) because the
                                 caller's IP is outside the tenant's
                                 allowlist.
    TENANT_IP_ALLOWLIST_UPDATED — the tenant's allowlist was updated
                                 via PUT /v1/tenant/security/ip-allowlist.

Without an `ALTER TYPE`, inserting either raises
`DataError: invalid input value for enum auditaction`. This mirrors
20261006t_fx90_malware_scan_audit_actions.py: one literal
`ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '<NAME>'` per value,
run on an autocommit connection because ADD VALUE cannot run inside a
transaction block. Idempotent — re-run is a no-op.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261006w_fx101_tenant_ip_allowlist.py            # dry run
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261006w_fx101_tenant_ip_allowlist.py --commit   # apply
"""
from __future__ import annotations

import argparse
import os
import sys

ENUM_NAME = "auditaction"
# One literal statement per new member — Python enum NAMES (uppercase), the
# stored form in this column, matching every other auditaction migration.
SYNC_STATEMENTS = [
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'TENANT_IP_BLOCKED'",
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'TENANT_IP_ALLOWLIST_UPDATED'",
]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    cur.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
