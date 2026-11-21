"""GY.C15b — add the `incident_export_jobs` table.

The incident export feature (services/incident_export.py) previously reused
the DownloadJob table, which could not hold the two things the feature needs:

  * the window the admin actually requested (run_job rebuilt
    `created_at -> now`, so a completed package covered a different window
    than the one requested);
  * the completed package's manifest sha256, which was being stuffed into
    `error_message`.

This table records both in their own columns. `tenant_id` is nullable (a
platform-wide export has no tenant). ISO 27001: no hard deletes — `deleted_at`
is the soft-delete marker, so unlike the append-only audit tables this table
does NOT get the append-only triggers (soft delete is an UPDATE by design).

The repo's migration runner is `tools/mod_migrate.py`, not Alembic: steps are
discovered as `*.py` files in this directory and run in filename order, each
recorded in the append-only `release_steps` ledger. Naming convention
`YYYYMMDD[_letter].slug.py` — this file sorts after 20261005c.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching the
database. The lead applies with --commit.

Run inside the API container:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261005d_gy_c15b_incident_export_jobs.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261005d_gy_c15b_incident_export_jobs.py --commit   # apply

Applied 2026-10-05.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "incident_export_jobs"

TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id VARCHAR PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants(id),
    requested_by INTEGER,
    window_start TIMESTAMP WITH TIME ZONE NOT NULL,
    window_end TIMESTAMP WITH TIME ZONE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    object_key TEXT,
    manifest_sha256 TEXT,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    completed_at TIMESTAMP WITH TIME ZONE,
    deleted_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT incident_export_jobs_window_ordered
        CHECK (window_end >= window_start)
);
"""

INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS ix_incident_export_jobs_tenant_id
    ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_incident_export_jobs_status
    ON {_TABLE} (status);
CREATE INDEX IF NOT EXISTS ix_incident_export_jobs_requested_by
    ON {_TABLE} (requested_by);
CREATE INDEX IF NOT EXISTS ix_incident_export_jobs_tenant_window
    ON {_TABLE} (tenant_id, window_start);
"""

_STATEMENTS = [
    (f"{_TABLE} table", TABLE_SQL),
    (f"indexes on {_TABLE}", INDEX_SQL),
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
            print(f"DRY  would: {label}")
            print(sql.rstrip())
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
    print(f"{_TABLE} ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
