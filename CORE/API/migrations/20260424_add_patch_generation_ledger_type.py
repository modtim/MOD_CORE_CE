"""Phase 5 — add PATCH_GENERATION value to the ledgerentrytype Postgres enum.

The self-healing variant patch loop (Phase 5) charges tenants a small
number of credits per LLM-generated patch proposal. That ledger entry
uses a new LedgerEntryType.PATCH_GENERATION enum value, distinct from
WORKFLOW_EXECUTION (DAG runs) and CREDIT_PURCHASE (Stripe) so the
admin dashboard can break out LLM patch cost separately.

Idempotent — a second run is a no-op. PostgreSQL's `ALTER TYPE ADD
VALUE IF NOT EXISTS` does the heavy lifting.

Run inside the API container:

    docker exec my_fastapi_app python3 migrations/20260424_add_patch_generation_ledger_type.py --commit

Dry run (default): prints what it would do.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List


ENUM_NAME = "ledgerentrytype"
NEW_VALUE = "PATCH_GENERATION"

# Values the enum shipped with in models.py (LedgerEntryType, models.py:177)
# before this step. PATCH_GENERATION is this migration's ADD VALUE; the rest
# are the pre-existing values create_all (tools/ce_migrate.py:125 ddl phase)
# normally stamps. STACK_RESERVATION (models.py LedgerEntryType, persistent
# stack billing) is in no migration, so it is listed here too and the
# existing-type path below adds it when missing. BURST_MANAGEMENT_FEE is NOT
# listed: migration 20260929 owns it. Only used to bootstrap the TYPE on a DB
# where create_all never ran (bare migration-chain runs); a DB that already has
# the type — every paid DB and every CE install — is left untouched (idempotent).
STACK_VALUE = "STACK_RESERVATION"
BASE_VALUES = ("CREDIT_PURCHASE", "WORKFLOW_EXECUTION", "REFUND", "ADJUSTMENT", STACK_VALUE)


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


def _type_exists(cur) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT typname FROM pg_type WHERE typname = %s)",
        (ENUM_NAME,),
    )
    return bool(cur.fetchone()[0])


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

    # Fresh-DB bootstrap (CE-NOPAID-R26.x fresh-DB proofs): the migration
    # chain can be run on a DB where create_all never ran, so the pg_type
    # does not exist and ALTER TYPE aborts. Create it — guarded by a pg_type
    # existence check (DO block) — BEFORE the ALTER, using the values
    # models.py shipped before this step plus this step's PATCH_GENERATION.
    # Idempotent: an existing DB (every paid DB, every CE install — create_all
    # stamps the type) leaves this DO block a no-op.
    fresh = not _type_exists(cur)
    create_stmt = (
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT typname FROM pg_type WHERE typname = "
        f"'{ENUM_NAME}') "
        "THEN CREATE TYPE "
        f"{ENUM_NAME} AS ENUM ("
        + ", ".join(f"'{v}'" for v in BASE_VALUES + (NEW_VALUE,))
        + "); END IF; END $$;"
    )
    print(f"{'WRITE' if args.commit else 'DRY  '}  {create_stmt}")
    if args.commit:
        cur.execute(create_stmt)

    if fresh:
        # PostgreSQL forbids ALTER TYPE ADD VALUE on a type created in the
        # same transaction, so the fresh path above already stamped the value.
        if not args.commit:
            print("(dry run — re-run with --commit to create the type)")
            return 0
        after = _existing_values(cur)
        if NEW_VALUE not in after:
            print(
                f"ERROR: {ENUM_NAME} created but {NEW_VALUE!r} not present; "
                f"got {after}",
                file=sys.stderr,
            )
            return 1
        print(f"Created {ENUM_NAME} from scratch. values now: {after}")
        return 0

    current = _existing_values(cur)
    print(f"Current {ENUM_NAME} values: {current}")

    # Existing DB: add PATCH_GENERATION (this migration) and STACK_RESERVATION
    # (a DB created before models.py gained it) when either is missing.
    missing = [v for v in (NEW_VALUE, STACK_VALUE) if v not in current]
    if not missing:
        print(f"{NEW_VALUE!r} and {STACK_VALUE!r} already present — nothing to do.")
        return 0

    stmts = [
        f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{v}'" for v in missing
    ]
    for stmt in stmts:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {stmt}")

    if args.commit:
        for stmt in stmts:
            cur.execute(stmt)
        after = _existing_values(cur)
        if any(v not in after for v in missing):
            print(
                f"ERROR: {missing} not present after ALTER TYPE; "
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
