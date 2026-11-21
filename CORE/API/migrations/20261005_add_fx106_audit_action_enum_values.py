"""FX.106 (owner Q93=A) — add FX.106 audit-action enum values.

Adds the three FX.106 values to the Postgres `auditaction` enum:
API_AUTO_BLOCK / API_AUTO_UNBLOCK (auto-block lifecycle, item 2+3) and
RATE_LIMIT_EXCEEDED (per-429 audit-of-record, item 1). Mirrors the SCIM
/ SSO enum migrations (ADD VALUE must run outside a transaction — the
caller connection is autocommit).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261005_add_fx106_audit_action_enum_values.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

FX106_AUDIT_VALUES = [
    "API_AUTO_BLOCK",
    "API_AUTO_UNBLOCK",
    "RATE_LIMIT_EXCEEDED",
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        for v in FX106_AUDIT_VALUES:
            print(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{v}';")
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: ALTER TYPE needs the owner role (CE has no modTech)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for v in FX106_AUDIT_VALUES:
        cur.execute(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{v}'")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print(f"Added {len(FX106_AUDIT_VALUES)} FX.106 audit values to "
          "auditaction enum.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
