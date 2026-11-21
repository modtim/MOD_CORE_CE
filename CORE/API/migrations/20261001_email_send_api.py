"""GX.S4 (launch-completion-megaplan Group X) — general email send API.

Adds ONE table so apps built on MOD can send email through the tenant's
existing SMTP config (TenantEmailConfig, resolved exactly as
utils.email_sender resolves it) via POST /v1/email/send.

  email_messages        one row per send. METADATA ONLY: recipient lists,
                        total size, attachment filename/mime/size — NEVER
                        attachment bytes or message bodies. Status
                        queued -> sent | failed; bounce/delivery updates
                        (usage metering seam) arrive
                        from the existing Postmark webhook matched on
                        provider_message_id. The row is NEVER
                        hard-deleted (ISO 27001 — SoftDeleteMixin).

`app_id` is a plain nullable integer — NO FK yet. The `apps` table (GX.A2)
does not exist yet; GX.A2 adds `REFERENCES apps(id)` + the index in its own
migration (same pattern as app_ephemeral_inputs).

Limits (max recipients / total size / attachment count / per-minute +
per-hour rate) are DB platform settings (services.platform_settings, keys
`email.max_recipients` / `email.max_total_size_bytes` /
`email.max_attachments` / `email.rate_limit_per_minute` /
`email.rate_limit_per_hour`), NOT env — this migration seeds no tunable
values; the registry defaults apply until an operator overrides them.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS. Safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261001_email_send_api.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("email_messages",)

CREATE_EMAIL_MESSAGES_SQL = """
CREATE TABLE IF NOT EXISTS email_messages (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- GX.A2 adds: REFERENCES apps(id). Plain nullable integer until the
    -- `apps` table exists.
    app_id              INTEGER,
    sender_user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    "to"                JSONB NOT NULL DEFAULT '[]',
    cc                  JSONB NOT NULL DEFAULT '[]',
    bcc                 JSONB NOT NULL DEFAULT '[]',
    subject             VARCHAR(512) NOT NULL,
    size_bytes          BIGINT NOT NULL DEFAULT 0,
    attachments         JSONB,
    status              VARCHAR(16) NOT NULL DEFAULT 'queued',
    error               TEXT,
    provider_message_id VARCHAR(512),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at             TIMESTAMPTZ,
    deleted_at          TIMESTAMPTZ
);
"""

INDEX_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_email_messages_tenant_id
    ON email_messages (tenant_id);
"""
INDEX_TENANT_CREATED_SQL = """
CREATE INDEX IF NOT EXISTS ix_email_messages_tenant_created
    ON email_messages (tenant_id, created_at DESC);
"""
INDEX_TENANT_STATUS_SQL = """
CREATE INDEX IF NOT EXISTS ix_email_messages_tenant_status
    ON email_messages (tenant_id, status);
"""
INDEX_PROVIDER_MESSAGE_ID_SQL = """
CREATE INDEX IF NOT EXISTS ix_email_messages_provider_message_id
    ON email_messages (tenant_id, provider_message_id);
"""

# AuditAction is a PG enum (`auditaction`) storing member NAMES.
ENUM_EMAIL_SENT_SQL = "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'EMAIL_SENT'"
ENUM_EMAIL_SEND_FAILED_SQL = (
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'EMAIL_SEND_FAILED'"
)

_STATEMENTS = [
    ("auditaction += EMAIL_SENT", ENUM_EMAIL_SENT_SQL),
    ("auditaction += EMAIL_SEND_FAILED", ENUM_EMAIL_SEND_FAILED_SQL),
    ("create table email_messages", CREATE_EMAIL_MESSAGES_SQL),
    ("index email_messages.tenant_id", INDEX_TENANT_SQL),
    ("index email_messages(tenant_id, created_at DESC)", INDEX_TENANT_CREATED_SQL),
    ("index email_messages(tenant_id, status)", INDEX_TENANT_STATUS_SQL),
    ("index email_messages(tenant_id, provider_message_id)", INDEX_PROVIDER_MESSAGE_ID_SQL),
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
    print("email_messages ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
