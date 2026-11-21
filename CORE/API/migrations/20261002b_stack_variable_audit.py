"""GX.S10a (launch-completion-megaplan Group X) — stack variable audit action.

Registers the ``STACK_VARIABLE_SET`` value on the Postgres ``auditaction``
enum type that backs ``models.AuditAction``. The value is emitted when a
tenant rotates a stack variable via ``PUT /stacks/{id}/variables/{name}``; the
audit row carries the variable NAME only (never the value, which lives in
Vault).

Additive only; idempotent: ``ADD VALUE IF NOT EXISTS``. Safe to re-run.

LEAD APPLIES — not applied by the task agent (the enum is shared live;
applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261002b_stack_variable_audit.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    ("auditaction += STACK_VARIABLE_SET",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'STACK_VARIABLE_SET'"),
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
    for label, sql in _STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    cur.close()
    print("stack variable audit action ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
