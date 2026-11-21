"""GSEC FX.77 part 1a — tenant log-stream (SIEM export) config table.

Adds the config table that backs tenant-configured SIEM export
destinations (models.TenantLogStream / services/log_streams.py): a
tenant can register a syslog/TLS, HTTPS webhook, Splunk HEC, or S3
export destination for its audit/security/run/auth events. Only
non-secret config lives in this table — the actual secret (TLS client
key, webhook bearer token, HEC token, S3 secret key) is referenced via
`secret_ref` (a Vault path, wired in part 1b) and never stored here.

tenant_log_streams (NEW):
    id             VARCHAR(36) PK        uuid4 string
    tenant_id      INTEGER NOT NULL      tenants.id FK
    name           VARCHAR(120) NOT NULL
    kind           VARCHAR(32) NOT NULL  syslog_tls|https_webhook|
                                         splunk_hec|s3_export
    endpoint       VARCHAR(512) NOT NULL
    config         JSONB NOT NULL        non-secret options (facility,
                                         format, batch size, S3 prefix)
    secret_ref     VARCHAR(256) NULL     Vault path; value never stored
    enabled        BOOLEAN NOT NULL
    event_types    JSONB NOT NULL        subset of audit|security|run|auth
    last_success_at / last_error_at TIMESTAMPTZ NULL
    last_error     VARCHAR(500) NULL
    created_by     INTEGER NULL          users.id FK
    created_at / updated_at / deleted_at (deleted_at = soft-delete
                                          marker, ISO 27001 — NO hard
                                          deletes)
    UNIQUE (tenant_id, name)

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS. Dry
run (default) PRINTS the SQL and exits 0 without touching the database.
Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007z_fx77_log_streams.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007z_fx77_log_streams.py --commit   # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order) — this file sorts after
20261007y.

models.py is bind-mounted live into the API container, so the table
must exist before any FX.77 part-1a code path writes to it — apply
immediately after adding the model to models.py. NOT applied by this
task (model/migration/tests only, p1a) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "tenant_log_streams table",
        """
CREATE TABLE IF NOT EXISTS tenant_log_streams (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    name VARCHAR(120) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    endpoint VARCHAR(512) NOT NULL,
    config JSONB NOT NULL DEFAULT '{}',
    secret_ref VARCHAR(256) NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    event_types JSONB NOT NULL DEFAULT '[]',
    last_success_at TIMESTAMPTZ NULL,
    last_error_at TIMESTAMPTZ NULL,
    last_error VARCHAR(500) NULL,
    created_by INTEGER NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_tenant_log_streams_tenant_name UNIQUE (tenant_id, name)
);
""",
    ),
    (
        "tenant_log_streams indexes",
        """
CREATE INDEX IF NOT EXISTS ix_tenant_log_streams_tenant_id
    ON tenant_log_streams (tenant_id);
CREATE INDEX IF NOT EXISTS ix_tenant_log_streams_kind
    ON tenant_log_streams (kind);
CREATE INDEX IF NOT EXISTS ix_tenant_log_streams_enabled
    ON tenant_log_streams (enabled);
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
    print("FX.77 tenant log stream schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
