# API/migrations/20261005u_fx93_abuse_audit_actions.py
"""FX.93 part 1 (owner decision Q79=A) — abuse-report audit actions.

Appends two new members to the `auditaction` Postgres enum (which backs
`models.AuditAction`):

  ABUSE_REPORT_CREATED  a user submitted an abuse report — metadata
                        carries the report id, target_type and
                        category (never the description text).
  ABUSE_REPORT_TRIAGED  a platform admin triaged a report — metadata
                        carries the report id + the transition
                        (dismissed / actioned / suspend_requested).
                        A 'suspend_target' decision only RECORDS the
                        requested suspension here; executing the
                        suspension is a later part (Planned).

Append-only audit rows (ISO 27001 — no hard delete).

`AuditAction` is a PG enum (`auditaction`) storing member NAMES
(uppercase), so the values are added with `ALTER TYPE auditaction ADD
VALUE IF NOT EXISTS '...'`. PG requires each ADD VALUE on a type that
is already in use to run in autocommit mode (no surrounding
transaction) — this script sets autocommit on the raw connection.

Purely additive — no table or data touched. Idempotent (ADD VALUE IF
NOT EXISTS). Safe to re-run.

Dry run (default, no --commit): prints the SQL and exits 0. Apply with
--commit, inside the API container:

    docker exec my_fastapi_app python3 \\
        migrations/20261005u_fx93_abuse_audit_actions.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

# AuditAction is a PG enum (`auditaction`) storing member NAMES.
_STATEMENTS = [
    ("auditaction += ABUSE_REPORT_CREATED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
     "'ABUSE_REPORT_CREATED'"),
    ("auditaction += ABUSE_REPORT_TRIAGED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
     "'ABUSE_REPORT_TRIAGED'"),
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
    print("fx93 abuse audit actions ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
