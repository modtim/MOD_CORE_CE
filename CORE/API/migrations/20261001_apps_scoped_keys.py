"""GX.A3 (launch-completion-megaplan Group X) — apps + scoped API keys.

Creates the ``apps`` table (one row per app built on MOD; the app's scope
lives on the row: allowed_workflow_ids / allowed_folder_ids), adds
``api_keys.app_id`` (a key with an app_id is an **app key**), and adds FK
constraints from ``email_messages.app_id`` and ``tenant_llm_calls.app_id``
to ``apps(id)`` where those columns exist. Also appends the two new
AuditAction enum values (AUTH_APP_SCOPE_DENIED, AUTH_API_KEY_EXPIRED) to
the ``auditaction`` PG enum.

``app_ephemeral_inputs`` is NOT created here — that is GX.A2/R4's table.

Additive only; existing keys have app_id NULL and behave exactly as today.
Idempotent: CREATE TABLE/INDEX IF NOT EXISTS, ADD COLUMN IF NOT EXISTS,
ADD VALUE IF NOT EXISTS, and DO-block guards on the FKs. Safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261001_apps_scoped_keys.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("apps",)

CREATE_APPS_SQL = """
CREATE TABLE IF NOT EXISTS apps (
    id                     SERIAL PRIMARY KEY,
    tenant_id              INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    slug                   VARCHAR(63),
    hostname               VARCHAR(253),
    name                   VARCHAR(255),
    -- provisioning | active | provisioning_failed | revoked
    status                 VARCHAR(32) NOT NULL DEFAULT 'provisioning',
    failure_detail         JSONB,
    -- stack-service | external-url
    target_kind            VARCHAR(16),
    stack_id               INTEGER REFERENCES persistent_stacks(id),
    stack_service          VARCHAR(127),
    stack_port             INTEGER,
    external_url           VARCHAR(1024),
    kc_client_id           VARCHAR(255) UNIQUE,
    kc_client_uuid         VARCHAR(36),
    api_key_id             INTEGER REFERENCES api_keys(id),
    allowed_workflow_ids   JSONB NOT NULL DEFAULT '[]',
    allowed_folder_ids     JSONB NOT NULL DEFAULT '[]',
    created_by_user_id     INTEGER REFERENCES users(id),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at             TIMESTAMPTZ
);
"""

INDEX_APPS_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_apps_tenant_id ON apps (tenant_id);
"""
INDEX_APPS_STACK_SQL = """
CREATE INDEX IF NOT EXISTS ix_apps_stack_id ON apps (stack_id);
"""
# ISO 27001: soft-delete only — uniqueness is partial (deleted rows keep
# their slug/hostname without colliding with a re-provisioned app).
UQ_APPS_TENANT_SLUG_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_apps_tenant_slug
    ON apps (tenant_id, slug) WHERE deleted_at IS NULL;
"""
UQ_APPS_HOSTNAME_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_apps_hostname
    ON apps (hostname) WHERE deleted_at IS NULL;
"""

ADD_API_KEY_APP_ID_SQL = """
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS app_id INTEGER REFERENCES apps(id);
"""
INDEX_API_KEYS_APP_ID_SQL = """
CREATE INDEX IF NOT EXISTS ix_api_keys_app_id ON api_keys (app_id);
"""

# FKs from the app_id columns GX.S4/GX.S5 added as plain nullable integers.
# Guarded: skip the table whose column is absent, and skip when a FK to
# apps(id) already exists (idempotent re-runs).
FK_EMAIL_MESSAGES_APP_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'email_messages' AND column_name = 'app_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'email_messages'::regclass
          AND c.contype = 'f'
          AND c.confrelid = 'apps'::regclass
    ) THEN
        ALTER TABLE email_messages
            ADD CONSTRAINT fk_email_messages_app_id
            FOREIGN KEY (app_id) REFERENCES apps(id);
    END IF;
END
$$;
"""
FK_TENANT_LLM_CALLS_APP_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenant_llm_calls' AND column_name = 'app_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'tenant_llm_calls'::regclass
          AND c.contype = 'f'
          AND c.confrelid = 'apps'::regclass
    ) THEN
        ALTER TABLE tenant_llm_calls
            ADD CONSTRAINT fk_tenant_llm_calls_app_id
            FOREIGN KEY (app_id) REFERENCES apps(id);
    END IF;
END
$$;
"""

# AuditAction is a PG enum (`auditaction`) storing member NAMES (uppercase).
# NOTE: ALTER TYPE ... ADD VALUE cannot run inside a transaction block, so
# each runs as its own autocommitted statement (see _get_conn).
ENUM_APP_SCOPE_DENIED_SQL = (
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'AUTH_APP_SCOPE_DENIED'"
)
ENUM_API_KEY_EXPIRED_SQL = (
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'AUTH_API_KEY_EXPIRED'"
)

_STATEMENTS = [
    ("auditaction += AUTH_APP_SCOPE_DENIED", ENUM_APP_SCOPE_DENIED_SQL),
    ("auditaction += AUTH_API_KEY_EXPIRED", ENUM_API_KEY_EXPIRED_SQL),
    ("create table apps", CREATE_APPS_SQL),
    ("index apps.tenant_id", INDEX_APPS_TENANT_SQL),
    ("index apps.stack_id", INDEX_APPS_STACK_SQL),
    ("unique apps(tenant_id, slug) WHERE deleted_at IS NULL", UQ_APPS_TENANT_SLUG_SQL),
    ("unique apps(hostname) WHERE deleted_at IS NULL", UQ_APPS_HOSTNAME_SQL),
    ("api_keys += app_id", ADD_API_KEY_APP_ID_SQL),
    ("index api_keys.app_id", INDEX_API_KEYS_APP_ID_SQL),
    ("email_messages.app_id -> apps(id)", FK_EMAIL_MESSAGES_APP_SQL),
    ("tenant_llm_calls.app_id -> apps(id)", FK_TENANT_LLM_CALLS_APP_SQL),
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
    for label, sql in _STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        for t in _TABLES:
            try:
                cur.execute(f'ALTER TABLE {t} OWNER TO {owner}')
            except Exception as e:  # noqa: BLE001
                print(f"(ALTER TABLE {t} OWNER TO {owner} failed: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("apps + scoped api keys ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
