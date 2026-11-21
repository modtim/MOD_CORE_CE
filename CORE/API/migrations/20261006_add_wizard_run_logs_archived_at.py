"""FX.57 part 1 (owner decision Q40=A) — wizard_run_logs.archived_at.

System-log retention: the nightly system-log archive job
(``services/system_log_archive.py``) exports ``wizard_run_logs`` rows
older than ``logs.searchable_days`` (default 90) as gzip JSONL + a
SHA-256 manifest into the platform ``system-logs`` object-storage
bucket, verifies the upload (size + sha256 via stat_object), and ONLY
then soft-marks the rows by stamping ``archived_at``.

This migration adds that single nullable column — purely additive,
every existing row gets NULL (= still in the online searchable
window, not yet archived). ISO 27001 no-hard-delete holds: archiving
is a STATUS CHANGE on the row, never a delete. Rows older than
``logs.archive_days`` (default 365; gov/DoD profiles require >= 548)
are soft-marked as well — mark only, never delete.

Idempotent: ``ADD COLUMN IF NOT EXISTS``; safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261006_add_wizard_run_logs_archived_at.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits
0 without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys


_TABLE = "wizard_run_logs"

ADD_ARCHIVED_AT_SQL = """
ALTER TABLE wizard_run_logs
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_wizard_run_logs_archived_at
    ON wizard_run_logs (archived_at);
"""

STATEMENTS = [
    ("add column wizard_run_logs.archived_at", ADD_ARCHIVED_AT_SQL),
    ("create index ix_wizard_run_logs_archived_at", CREATE_INDEX_SQL),
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
        for label, sql in STATEMENTS:
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        try:
            cur.execute(
                f'ALTER TABLE {_TABLE} OWNER TO {owner}')
        except Exception as e:  # noqa: BLE001
            print(f"(ALTER TABLE {_TABLE} OWNER TO {owner} failed: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("wizard_run_logs.archived_at ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
