#!/usr/bin/env python3
"""BYO move-out MIG-0 -- the ONE new table: STORAGE MOVE JOBS (one row per
move of a tenant's objects from the house store to the tenant's own S3
bucket; design results-1110/DESIGN-BYO-MOVE-OUT-FLOW.md section 6).

revision      : 20261111a_storage_move_jobs
down_revision : 20261109b_tenant_email_smtp_security

Why a new table: reusing StorageConnection.migration_* for a move-out would
collide with migrate-on-activate on the same row. No new column on
content_blobs and no blob->connection FK: "which store serves me" stays the
active connection (resolve_backend).

Columns:
    id                 BIGSERIAL PRIMARY KEY (never a max()-subquery
                       DEFAULT -- Postgres rejects subqueries in DEFAULT)
    tenant_id          INTEGER REFERENCES tenants (id) ON DELETE SET NULL
    connection_id      INTEGER REFERENCES storage_connections (id)
                       ON DELETE SET NULL
    target_bucket      VARCHAR
    status             VARCHAR(16) NOT NULL DEFAULT 'pending', CHECK in
                       pending|running|verified|failed|cancelled
    total_objects / moved_object_count / moved_bytes
                       BIGINT NOT NULL DEFAULT 0
    chunks_total / chunks_done
                       INTEGER NOT NULL DEFAULT 0
    error              TEXT
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
    completed_at       TIMESTAMPTZ

Indexes: (tenant_id, connection_id) and (status).

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. No delete anywhere
(soft delete / no hard delete: job rows are history).

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261111a_storage_move_jobs.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "storage_move_jobs"

_STATEMENTS = [
    (
        f"{_TABLE} table (BYO move-out job tracking)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants (id) ON DELETE SET NULL,
    connection_id INTEGER REFERENCES storage_connections (id) ON DELETE SET NULL,
    target_bucket VARCHAR,
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK
        (status IN ('pending', 'running', 'verified', 'failed', 'cancelled')),
    total_objects BIGINT NOT NULL DEFAULT 0,
    moved_object_count BIGINT NOT NULL DEFAULT 0,
    moved_bytes BIGINT NOT NULL DEFAULT 0,
    chunks_total INTEGER NOT NULL DEFAULT 0,
    chunks_done INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
""",
    ),
    (
        f"{_TABLE} index (tenant_id, connection_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_connection
    ON {_TABLE} (tenant_id, connection_id);
""",
    ),
    (
        f"{_TABLE} index (status)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_status
    ON {_TABLE} (status);
""",
    ),
]


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
                   help="Apply the CREATE TABLE + index steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (move-job rows are kept as history; "
              "nothing is dropped on rollback)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role -- resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
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
    print("BYO move-out MIG-0 ready: storage_move_jobs exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
