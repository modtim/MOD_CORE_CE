"""FX.117 (Q104=A) — add SECRET_INJECTED audit-action enum value.

One row per secret USE at run-time injection (worker run-token path
GET /secrets/by-name/{name}/value): who/what run/node/secret id — never
the value. Mirrors the FX.106 audit-action enum migration: ADD VALUE must
run outside a transaction (the caller connection is autocommit).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261005m_fx117_secret_injection_audit.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

FX117_AUDIT_VALUES = [
    "SECRET_INJECTED",
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
        for v in FX117_AUDIT_VALUES:
            print(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{v}';")
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: ALTER TYPE needs the owner role (CE has no modTech)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for v in FX117_AUDIT_VALUES:
        cur.execute(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{v}'")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print(f"Added {len(FX117_AUDIT_VALUES)} FX.117 audit values to "
          "auditaction enum.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
