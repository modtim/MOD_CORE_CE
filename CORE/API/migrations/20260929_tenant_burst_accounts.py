"""GH.2 — tenant_burst_accounts: per-tenant cloud credential accounts for
bursting into the CUSTOMER's own cloud account.

tenant_burst_accounts — one row per (tenant, provider), LIVE rows only
unique (partial index WHERE deleted_at IS NULL):
  provider     registered burst-provider name (services/burst_provider.py
               registry key: 'fake' in dev/tests, 'libcloud' for real clouds)
  secret_ref   NAME of the tenant secret holding the cloud credential —
               NEVER the credential itself. Resolved at probe/provision
               time via the existing tenant secret store.
  region       provider region (e.g. us-east-1)
  limits       JSONB spend/policy block: {max_instances,
               max_gpu_hours_month, allowed_instance_types}
  status       'pending' | 'valid' | 'invalid' (last dry-run permission
               probe; last_probe_at / last_probe_error record the outcome,
               the error string is redacted — never contains a secret)

ISO 27001: soft-delete only (deleted_at); rows are never hard-deleted and
every mutation is audit-logged.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS; safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260929_tenant_burst_accounts.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_TENANT_BURST_ACCOUNTS_SQL = """
CREATE TABLE IF NOT EXISTS tenant_burst_accounts (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id),
    provider            VARCHAR(50) NOT NULL,
    secret_ref          VARCHAR(255) NOT NULL,
    region              VARCHAR(100),
    limits              JSONB,
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',
    last_probe_at       TIMESTAMPTZ,
    last_probe_error    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);
"""

# Partial unique index: one LIVE account per (tenant, provider).
# Soft-deleted rows sit outside the index, so delete-then-recreate works.
UQ_TENANT_BURST_ACCOUNTS_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_burst_account_live_provider
    ON tenant_burst_accounts (tenant_id, provider)
    WHERE deleted_at IS NULL;
"""

INDEX_TENANT_BURST_ACCOUNTS_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_tenant_burst_accounts_tenant_id
    ON tenant_burst_accounts (tenant_id);
"""

STATEMENTS = [
    CREATE_TENANT_BURST_ACCOUNTS_SQL,
    UQ_TENANT_BURST_ACCOUNTS_SQL,
    INDEX_TENANT_BURST_ACCOUNTS_TENANT_SQL,
]

TABLES = ["tenant_burst_accounts"]


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
        for s in STATEMENTS:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    # Create objects owned by modTech (not the transient Vault dynamic role
    # that runs this migration) so the tables match every other table's
    # ownership and the API's startup ALTER-OWNER reconcile is a no-op
    # instead of failing with "must be owner". The app's dynamic role is a
    # member of modTech, so SET ROLE succeeds; fall back to plain execution
    # if it can't (dev/sqlite).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for s in STATEMENTS:
        cur.execute(s)
    if set_role_ok:
        for t in TABLES:
            try:
                cur.execute(f'ALTER TABLE {t} OWNER TO {owner}')
            except Exception as e:  # noqa: BLE001
                print(f"(ALTER OWNER skipped for {t}: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("tenant_burst_accounts table ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
