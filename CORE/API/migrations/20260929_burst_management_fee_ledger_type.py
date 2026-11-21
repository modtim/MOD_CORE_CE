"""GH.4 — add BURST_MANAGEMENT_FEE value to the ledgerentrytype Postgres enum.

Burst metering (services/burst_metering.py) debits the customer's prepaid
credit balance for the flat management fee MOD charges per managed
burst-GPU-hour (decomp/gh-burst.md, GH.9 pricing). That ledger entry uses a
new LedgerEntryType.BURST_MANAGEMENT_FEE enum value, distinct from
WORKFLOW_EXECUTION and STACK_RESERVATION, so the admin dashboard / ledger
can break out burst management spend separately.

Idempotent — a second run is a no-op. PostgreSQL's `ALTER TYPE ADD VALUE
IF NOT EXISTS` does the heavy lifting.

Run inside the API container:

    docker exec my_fastapi_app python3 migrations/20260929_burst_management_fee_ledger_type.py --commit

Dry run (default): prints what it would do.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List


ENUM_NAME = "ledgerentrytype"
NEW_VALUE = "BURST_MANAGEMENT_FEE"


def _get_psycopg_cursor():
    """Reuse the API's configured SQLAlchemy engine (Vault-dynamic
    creds + SSL posture) instead of parsing DATABASE_URL ourselves.
    Run this migration from inside the API container where `database`
    is importable.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError(
            "Database engine still None after initialize() — check "
            "Vault credentials in this container."
        )
    # Exit any implicit SQLAlchemy transaction — ALTER TYPE ADD VALUE
    # cannot run inside a BEGIN...COMMIT block. SQLAlchemy's
    # raw_connection() returns a pool proxy; drill to the underlying
    # psycopg2 connection before flipping autocommit.
    raw = eng.raw_connection()
    dbapi_conn = getattr(raw, "driver_connection", None) or \
        getattr(raw, "connection", None) or raw
    dbapi_conn.autocommit = True
    return dbapi_conn


def _existing_values(cur) -> List[str]:
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
    ap.add_argument(
        "--commit", action="store_true",
        help="Apply the ALTER TYPE. Without this flag, dry-run only.",
    )
    args = ap.parse_args()

    conn = _get_psycopg_cursor()
    cur = conn.cursor()

    current = _existing_values(cur)
    print(f"Current {ENUM_NAME} values: {current}")

    if NEW_VALUE in current:
        print(f"{NEW_VALUE!r} already present — nothing to do.")
        return 0

    stmt = f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{NEW_VALUE}'"
    print(f"{'WRITE' if args.commit else 'DRY  '}  {stmt}")

    if args.commit:
        cur.execute(stmt)
        after = _existing_values(cur)
        if NEW_VALUE not in after:
            print(
                f"ERROR: {NEW_VALUE!r} not present after ALTER TYPE; "
                f"got {after}",
                file=sys.stderr,
            )
            return 1
        print(f"Applied. {ENUM_NAME} now: {after}")
    else:
        print("(dry run — re-run with --commit to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
