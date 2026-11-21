"""GY.C14 — add the PLATFORM_UPDATE_APPLIED audit-action enum value.

`update.sh` records each applied platform update as one security-audit row
(`audit.log_security_event` with models.AuditAction.PLATFORM_UPDATE_APPLIED).
The `auditaction` Postgres enum must gain the value outside a transaction
block — this script handles that (mirrors
20260605_add_scim_audit_action_enum_values.py).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261003g_gy_c14_platform_update_audit.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

NEW_VALUE = "platform_update_applied"


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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        print(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{NEW_VALUE}';")
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: ALTER TYPE needs the owner role (CE has no modTech)
    set_role_ok = _owner_role.maybe_set_role(cur)
    cur.execute(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{NEW_VALUE}'")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print(f"Added audit-action enum value {NEW_VALUE!r} to auditaction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
