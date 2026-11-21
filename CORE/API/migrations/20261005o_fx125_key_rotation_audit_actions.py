"""GSEC FX.125 PART 1 (owner decision Q112=A) — platform key rotation.

Appends two new members to the `auditaction` Postgres enum (which
backs `models.AuditAction`):

  KEY_ROTATION_OVERDUE  the daily 04:52 UTC sweep (services/scheduler.py
                        + services/key_inventory.py) found a platform
                        key / cert / enrollment token past its keys.*
                        rotation-schedule setting.
  KEY_ROTATED           the part-2 rotation jobs record each completed
                        rotation (transit rotate, realm key rollover,
                        cert re-issue, token re-issue, master-key
                        re-wrap).

`AuditAction` is a PG enum (`auditaction`) storing member NAMES
(uppercase), so the values are added with `ALTER TYPE auditaction ADD
VALUE IF NOT EXISTS '...'`.

Purely additive — no table or data touched. Idempotent (ADD VALUE IF
NOT EXISTS). Safe to re-run.

Applied 2026-10-05 (FX.125 part 1, per task instruction):

    docker exec my_fastapi_app python3 \
        migrations/20261005o_fx125_key_rotation_audit_actions.py --commit

Dry run (default, no --commit): prints the SQL and exits 0.
"""
from __future__ import annotations

import argparse
import os
import sys

# AuditAction is a PG enum (`auditaction`) storing member NAMES.
ENUM_KEY_ROTATION_OVERDUE_SQL = (
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'KEY_ROTATION_OVERDUE'"
)
ENUM_KEY_ROTATED_SQL = (
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'KEY_ROTATED'"
)

_STATEMENTS = [
    ("auditaction += KEY_ROTATION_OVERDUE", ENUM_KEY_ROTATION_OVERDUE_SQL),
    ("auditaction += KEY_ROTATED", ENUM_KEY_ROTATED_SQL),
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
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
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
    print("fx125 key-rotation audit actions ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
