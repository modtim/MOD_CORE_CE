"""GY.C5 (launch-completion-megaplan) — add SIEM_EXPORT_FAILING to the
auditaction enum.

services/siem_export.py writes one SIEM_EXPORT_FAILING security event
per SIEM outage (NIST AU-5 alert): the shipper could not deliver audit
rows (send failure / egress refusal / missing Vault auth token).

Additive only; idempotent (ALTER TYPE ... ADD VALUE IF NOT EXISTS).
Safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261003_gy_c5_siem_export_failing.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys


ENUM_NAME = "auditaction"
# SQLAlchemy stores Python enum *names* (uppercase) in this column.
NEW_VALUES = ["SIEM_EXPORT_FAILING"]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def _existing_values(cur) -> list:
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

    conn = _get_conn()
    cur = conn.cursor()
    current = _existing_values(cur)

    to_add = [v for v in NEW_VALUES if v not in current]
    if not to_add:
        print(f"All values already present — nothing to do.")
        return 0

    for v in to_add:
        stmt = f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{v}'"
        print(f"{'WRITE' if args.commit else 'DRY  '}  {stmt}")
        if args.commit:
            cur.execute(stmt)

    if args.commit:
        after = _existing_values(cur)
        print(f"Applied. {ENUM_NAME} now contains {len(after)} values "
              f"(added: {', '.join(to_add)}).")
    else:
        print("(dry run — re-run with --commit to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
