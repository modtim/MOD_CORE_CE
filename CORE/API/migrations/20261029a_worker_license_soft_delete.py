#!/usr/bin/env python3
"""BUG-1029-worker-hard-delete (ISO 27001 no-hard-delete) — soft-delete columns
for `worker_licenses` + `worker_enrollments`.

revision      : 20261029a_worker_license_soft_delete
down_revision : 20261028a_stack_deploy_idempotency_keys

`DELETE /workers/{name}` used to hard-delete the WorkerLicense row (plus its
WorkerEnrollment row and the WorkerAssignmentHistoryRecord / WorkerResourceTimeline
cascades). ISO 27001 forbids hard deletes of product data, so the delete path now
tombstones instead: it stamps `deleted_at` + `deleted_by_user_id` and keeps every
history row. These two tables had no `deleted_at` column at all, so the fix needs
one.

Additive only: two nullable columns (`deleted_at`, `deleted_by_user_id`) per table
+ three non-unique B-tree indexes (`ix_*_deleted_at`, `ix_worker_licenses_deleted_by_user_id`,
`ix_worker_enrollments_deleted_at`), no backfill, no UPDATE, no existing column or
index changed.

`worker_name` is NOT relaxed here. `persistent_stacks.target_worker` and
`worker_resource_timelines.worker_name` are FKs to `worker_licenses.worker_name`,
and Postgres requires a PLAIN UNIQUE constraint as an FK target — a partial unique
index (`WHERE deleted_at IS NULL`) cannot satisfy one, so swapping it breaks every
FK-bearing CREATE TABLE. Re-enrolling a decommissioned worker under the SAME name
therefore needs the tombstone reactivated in the enrollment path (services scope,
the iso-no-hard-delete follow-up); the row itself is never reaped and stays
auditable, which is what this bug is about.

Idempotent: every step is IF NOT EXISTS / guarded, re-running is a no-op.
Reversible: `--downgrade [--commit]` drops the soft-delete columns from each table;
the original plain unique constraint (`worker_name`) is unaffected because the
migration only adds columns/indexes — it never replaces an existing unique constraint.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the lane
— the lead applies it:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261029a_worker_license_soft_delete.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_COLUMNS = [
    (
        "worker_licenses.deleted_at column",
        """
ALTER TABLE worker_licenses
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
""",
    ),
    (
        "worker_licenses.deleted_by_user_id column (FK, ON DELETE SET NULL)",
        """
ALTER TABLE worker_licenses
    ADD COLUMN IF NOT EXISTS deleted_by_user_id INTEGER
        REFERENCES users(id) ON DELETE SET NULL;
""",
    ),
    (
        "worker_enrollments.deleted_at column",
        """
ALTER TABLE worker_enrollments
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
""",
    ),
    (
        "worker_enrollments.deleted_by_user_id column (FK, ON DELETE SET NULL)",
        """
ALTER TABLE worker_enrollments
    ADD COLUMN IF NOT EXISTS deleted_by_user_id INTEGER
        REFERENCES users(id) ON DELETE SET NULL;
""",
    ),
    (
        "ix_worker_licenses_deleted_at index",
        """
CREATE INDEX IF NOT EXISTS ix_worker_licenses_deleted_at
    ON worker_licenses (deleted_at);
""",
    ),
    (
        "ix_worker_licenses_deleted_by_user_id index",
        """
CREATE INDEX IF NOT EXISTS ix_worker_licenses_deleted_by_user_id
    ON worker_licenses (deleted_by_user_id);
""",
    ),
    (
        "ix_worker_enrollments_deleted_at index",
        """
CREATE INDEX IF NOT EXISTS ix_worker_enrollments_deleted_at
    ON worker_enrollments (deleted_at);
""",
    ),
]

_DOWNGRADE = """
ALTER TABLE worker_licenses DROP COLUMN IF EXISTS deleted_at;
ALTER TABLE worker_licenses DROP COLUMN IF EXISTS deleted_by_user_id;
ALTER TABLE worker_enrollments DROP COLUMN IF EXISTS deleted_at;
ALTER TABLE worker_enrollments DROP COLUMN IF EXISTS deleted_by_user_id;
"""


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the DDL. Without this flag, dry-run only.")
    p.add_argument("--downgrade", action="store_true",
                   help="Drop the columns + restore plain unique indexes (dry-run unless --commit).")
    args = p.parse_args()

    if args.downgrade:
        if not args.commit:
            print("DRY  would: " + _DOWNGRADE)
            return 0
        conn = _get_conn()
        cur = conn.cursor()
        for stmt in [s.strip() for s in _DOWNGRADE.split(";") if s.strip()]:
            try:
                cur.execute(stmt + ";")
            except Exception as e:  # noqa: BLE001
                print(f"FAIL downgrade: {e}")
                return 1
        cur.close()
        conn.close()
        print("OK   downgrade: worker soft-delete columns dropped.")
        return 0

    if not args.commit:
        for label, sql in _COLUMNS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade available: --downgrade [--commit]")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — SET ROLE only if it exists
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _COLUMNS:
        try:
            cur.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
