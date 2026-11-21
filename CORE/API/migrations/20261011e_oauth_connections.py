#!/usr/bin/env python3
"""sz-4.1 (sz-productivity-floor decomp §4.1) — the ONE new table:
OAUTH CONNECTIONS (the credential vault for every external-provider account
a tenant has connected: google / github / notion / linear / slack /
airtable / microsoft365).

revision      : 20261011e_oauth_connections
down_revision : 20261011d_output_delivery_log

20261011d_output_delivery_log is the CURRENT head: no other migration
names it as its down_revision (verified by grep over migrations/), so this
row chains onto it.

Model + migration only — NO broker, NO OAuth flows (that is sz-4.2). This
table is where whatever the broker later mints lands.

Columns, per the sz-4.1 decomp row:
    id                      BIGSERIAL PRIMARY KEY
    tenant_id               INTEGER NOT NULL REFERENCES tenants (id)
                            — NO ON DELETE action (matching the other
                            audit/vault tables in models.py: CostRouting
                            Decision, WorkflowOutputAuditLog, AuditEvent,
                            PermissionAuditLog, PlatformSettingAudit —
                            none cascade): a tenant row cannot be hard-
                            deleted out from under the record of what it
                            once connected.
    user_id                 INTEGER NULL REFERENCES users (id)
                            — NO ON DELETE action either; NULL = a
                            tenant-wide connection with no single owner.
    provider                VARCHAR(32) NOT NULL
                            CHECK (provider IN ('google','github','notion',
                            'linear','slack','airtable','microsoft365'))
    account_label           VARCHAR(255) NULL
                            — REDACTED display string: the masked email or
                            handle the UI shows ("a•••d@acme.com", "octocat").
    external_account_id     VARCHAR(255) NULL
                            — the provider's opaque account id, used only
                            by the partial UNIQUE guard below; never
                            rendered.
    scopes                  JSONB NOT NULL DEFAULT '[]'::jsonb
    encrypted_access_token  TEXT NULL
    encrypted_refresh_token TEXT NULL
                            — CIPHERTEXT ONLY, same convention as
                            services/http_triggers.encrypt_secret: the
                            value is utils.encryption.encrypt_value(plain,
                            <tenant key>) with key material from
                            services/key_provider (per-TENANT key).
                            Plaintext tokens never live in this table, a
                            log line, or the model's repr.
    key_version             VARCHAR(32) NULL
                            — which tenant-key generation minted the
                            ciphertext, so a rotation tells the refresher
                            to demand a re-connect instead of a retry.
    access_token_expires_at TIMESTAMPTZ NULL
    last_refreshed_at       TIMESTAMPTZ NULL
    status                  VARCHAR(24) NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','refresh_failed',
                            'reconnect_required','revoked'))
    last_error_code         VARCHAR(64) NULL
                            — CODE ONLY ("invalid_grant", "key_rotated");
                            NEVER a provider response body, which is where
                            providers leak account metadata.
    created_at / updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    deleted_at              TIMESTAMPTZ NULL
    deleted_by              INTEGER NULL
                            — ISO 27001 soft delete: a disconnected
                            connection gets these stamped and stays in the
                            table; nothing is ever hard-deleted.

Indexes: ix_oauth_connections_tenant_provider on (tenant_id, provider);
the PARTIAL UNIQUE uq_oauth_connections_live_external on (tenant_id,
provider, external_account_id) WHERE deleted_at IS NULL AND external_
account_id IS NOT NULL — at most one LIVE connection per external account,
while a tombstone never blocks a re-connect and rows with no external id
are outside the guard; and ix_oauth_connections_status_expiry on
(status, access_token_expires_at) for the pre-expiry refresh job.

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.
No ON DELETE clause anywhere — no CASCADE, no ONDELETE (a glued ONDELETE is
a Postgres syntax error anyway).

Idempotent: CREATE TABLE / CREATE INDEX / CREATE UNIQUE INDEX IF NOT
EXISTS; safe to re-run. Additive only: no drops, no ALTER on existing
tables. A matching models.py ORM class (`OAuthConnection`) is added
alongside this migration.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261011e_oauth_connections.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "oauth_connections"
_REVISION = "20261011e_oauth_connections"
_DOWN_REVISION = "20261011d_output_delivery_log"
_ALLOWED_PROVIDERS = ("google", "github", "notion", "linear", "slack",
                      "airtable", "microsoft365")
_ALLOWED_STATUSES = ("active", "refresh_failed", "reconnect_required",
                     "revoked")

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-4.1, external-provider credential vault)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    user_id INTEGER REFERENCES users (id),
    provider VARCHAR(32) NOT NULL
        CHECK (provider IN ('google', 'github', 'notion', 'linear',
                            'slack', 'airtable', 'microsoft365')),
    account_label VARCHAR(255),
    external_account_id VARCHAR(255),
    scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    encrypted_access_token TEXT,
    encrypted_refresh_token TEXT,
    key_version VARCHAR(32),
    access_token_expires_at TIMESTAMPTZ,
    last_refreshed_at TIMESTAMPTZ,
    status VARCHAR(24) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'refresh_failed',
                          'reconnect_required', 'revoked')),
    last_error_code VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    deleted_by INTEGER
);
""",
    ),
    (
        f"{_TABLE} index (tenant_id, provider)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_provider
    ON {_TABLE} (tenant_id, provider);
""",
    ),
    (
        f"{_TABLE} partial UNIQUE (tenant_id, provider, external_account_id)"
        " WHERE live AND has an external id",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live_external
    ON {_TABLE} (tenant_id, provider, external_account_id)
    WHERE deleted_at IS NULL AND external_account_id IS NOT NULL;
""",
    ),
    (
        f"{_TABLE} index (status, access_token_expires_at)"
        " for the pre-expiry refresh job",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_status_expiry
    ON {_TABLE} (status, access_token_expires_at);
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
                   help="Apply the CREATE TABLE + index steps. "
                        "Without this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (connection rows are credential "
              "history kept per ISO 27001 — no destructive rollback "
              "defined)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
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
    print(
        "sz-4.1 ready: oauth_connections exists; the OAuth broker (sz-4.2, "
        "a later decomp row) writes it. The lead applies --commit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
