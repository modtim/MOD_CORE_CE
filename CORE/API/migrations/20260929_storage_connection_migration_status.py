"""WS4.Q17.1 — migrate-on-activate status columns on storage_connections.

When a tenant swaps their active BYO-S3 connection, existing objects under
the OLD backend get copied to the NEW one before cutover. These columns let
that background migration report its progress; they are read-only from the
API's perspective (set by the migration worker) and are exposed on the
StorageConnection response schema (schemas.py) so the Dashboard can poll
status without a separate endpoint.

Adds SIX nullable/defaulted columns to `storage_connections`:

    migration_status       VARCHAR(16) NOT NULL DEFAULT 'none'
                            one of: none | pending | running | verified | failed
                            (CHECK constraint enforces the enum)
    migration_started_at   TIMESTAMPTZ NULL
    migration_completed_at TIMESTAMPTZ NULL
    migrated_object_count  BIGINT NOT NULL DEFAULT 0
    migrated_bytes         BIGINT NOT NULL DEFAULT 0
    migration_error        TEXT NULL

Purely additive — no existing data touched (every existing row becomes
migration_status='none', migrated_object_count=0, migrated_bytes=0, the
correct "never migrated" state for connections that predate this feature).

Idempotent: every step is guarded (`ADD COLUMN IF NOT EXISTS` / a
pg_constraint existence check before adding the CHECK); safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260929_storage_connection_migration_status.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys


TABLE = "storage_connections"
CHECK_CONSTRAINT_NAME = "ck_storage_connection_migration_status"

ADD_COLUMNS_SQL = f"""
ALTER TABLE {TABLE}
    ADD COLUMN IF NOT EXISTS migration_status VARCHAR(16) NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS migration_started_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS migration_completed_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS migrated_object_count BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS migrated_bytes BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS migration_error TEXT NULL;
"""

ADD_CHECK_SQL = (
    f"ALTER TABLE {TABLE} "
    f"ADD CONSTRAINT {CHECK_CONSTRAINT_NAME} "
    "CHECK (migration_status IN ('none', 'pending', 'running', 'verified', 'failed'))"
)


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


def _constraint_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (name,))
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="Apply the DDL. Without this flag, dry-run only.")
    args = ap.parse_args()

    if not args.commit:
        print(ADD_COLUMNS_SQL)
        print(f"-- (only if not already present) {ADD_CHECK_SQL};")
        print("(dry run — re-run with --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)

    cur.execute(ADD_COLUMNS_SQL)
    print(f"WRITE  {ADD_COLUMNS_SQL.strip()}")

    if not _constraint_exists(cur, CHECK_CONSTRAINT_NAME):
        cur.execute(ADD_CHECK_SQL)
        print(f"WRITE  {ADD_CHECK_SQL}")
    else:
        print(f"{CHECK_CONSTRAINT_NAME} already present — skipped.")

    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("storage_connections migration-status columns ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
