"""GSEC FX.59 part 3 (owner decision Q42=A) — self-serve tenant export job.

Adds the job table that backs the tenant-admin self-serve full export
(models.TenantExportJob / services/tenant_export.py): during the
contract-end export window (tenant lifecycle `read_only`, for
`tenant_lifecycle.export_window_days` after contract end) a tenant admin
can zip their whole workspace — workflows, files, outputs, audit log —
stored in the tenant's own bucket under `exports/`, and download it via a
short-lived presigned link.

tenant_export_jobs (NEW):
    id            VARCHAR(36) PK        uuid4 string
    tenant_id     INTEGER NOT NULL      tenants.id FK
    requested_by  INTEGER NULL          users.id (no FK: audit rows can be
                                        recorded for user 0)
    status        VARCHAR(32) NOT NULL  pending|building|uploading|
                                        completed|failed
    object_key    VARCHAR(512) NULL     exports/<id>/package.zip
    size_bytes    BIGINT NULL
    expires_at    TIMESTAMPTZ NULL      download-link / package expiry
    error         VARCHAR(500) NULL     failure text only
    created_at / completed_at / deleted_at (deleted_at = soft-delete
                                          marker, ISO 27001 — NO hard
                                          deletes)

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    TENANT_EXPORT_REQUESTED / TENANT_EXPORT_DOWNLOADED

Metadata on those rows carries the job id / object key only — never
package content.

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS +
ALTER TYPE ADD VALUE IF NOT EXISTS. Dry run (default) PRINTS the SQL
and exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007r_fx59_tenant_export.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007r_fx59_tenant_export.py --commit   # apply

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction
block, so the whole script runs on an autocommit connection (same
pattern as 20261007k_fx50_security_incidents.py). Migration runner is
tools/mod_migrate.py (steps discovered as *.py in this directory, run
in filename order) — this file sorts after 20261007q.

models.py is bind-mounted live into the API container, so the table +
enum values must exist before any FX.59 part-3 code path writes to them
— apply immediately after adding the models to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "tenant_export_jobs table",
        """
CREATE TABLE IF NOT EXISTS tenant_export_jobs (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    requested_by INTEGER NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    object_key VARCHAR(512) NULL,
    size_bytes BIGINT NULL,
    expires_at TIMESTAMPTZ NULL,
    error VARCHAR(500) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);
""",
    ),
    (
        "tenant_export_jobs indexes",
        """
CREATE INDEX IF NOT EXISTS ix_tenant_export_jobs_tenant_id
    ON tenant_export_jobs (tenant_id);
CREATE INDEX IF NOT EXISTS ix_tenant_export_jobs_requested_by
    ON tenant_export_jobs (requested_by);
CREATE INDEX IF NOT EXISTS ix_tenant_export_jobs_status
    ON tenant_export_jobs (status);
CREATE INDEX IF NOT EXISTS ix_tenant_export_jobs_tenant_created
    ON tenant_export_jobs (tenant_id, created_at);
""",
    ),
    (
        "auditaction tenant export values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'TENANT_EXPORT_REQUESTED';
""",
    ),
    (
        "auditaction tenant export values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'TENANT_EXPORT_DOWNLOADED';
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
        # one statement per semicolon — the batch may hold several
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.59 tenant export job schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
