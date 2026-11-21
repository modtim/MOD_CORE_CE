# API/migrations/20261005s_fx140_consent_audit_action.py
"""GSEC FX.140 PART 3 (owner decision Q128=A) — email-consent audit action.

Appends one new member to the `auditaction` Postgres enum (which backs
`models.AuditAction`):

  EMAIL_CONSENT_CHANGED   a consent decision was written: the sign-up
                          marketing opt-in checkbox, the Dashboard
                          preferences PUT, or a one-click unsubscribe
                          link click. Audit metadata = {category,
                          granted, source} — never the email address
                          (PII). Append-only audit rows (ISO 27001 —
                          no hard delete).

`AuditAction` is a PG enum (`auditaction`) storing member NAMES
(uppercase), so the value is added with `ALTER TYPE auditaction ADD
VALUE IF NOT EXISTS '...'`.

Purely additive — no table or data touched. Idempotent (ADD VALUE IF
NOT EXISTS). Safe to re-run.

Applied 2026-10-05 (FX.140 part 3, per task instruction):

    docker exec my_fastapi_app python3 \
        migrations/20261005s_fx140_consent_audit_action.py --commit

Dry run (default, no --commit): prints the SQL and exits 0.
"""
from __future__ import annotations

import argparse
import os
import sys

# AuditAction is a PG enum (`auditaction`) storing member NAMES.
ENUM_EMAIL_CONSENT_CHANGED_SQL = (
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
    "'EMAIL_CONSENT_CHANGED'"
)

_STATEMENTS = [
    ("auditaction += EMAIL_CONSENT_CHANGED",
     ENUM_EMAIL_CONSENT_CHANGED_SQL),
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
    print("fx140 consent audit action ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
