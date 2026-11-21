"""GX.R3 — run-completion webhooks: subscriptions, deliveries, attempt log.

Adds three tables + three audit-action enum values so external apps are
notified (signed) when a workflow run reaches a terminal state instead of
polling:

  run_webhook_subscriptions       per-workflow, tenant-scoped, soft-delete;
                                  HMAC signing secret stored tenant-key-
                                  encrypted (shown once at create/rotate).
  run_webhook_deliveries          one row per (target, run terminal
                                  transition); current state + retry pointer.
  run_webhook_delivery_attempts   append-only per-attempt log (ISO 27001:
                                  never updated/deleted); no response bodies.

auditaction enum gains:
  RUN_WEBHOOK_SUBSCRIPTION_CREATED
  RUN_WEBHOOK_SUBSCRIPTION_DELETED
  RUN_WEBHOOK_SECRET_ROTATED
(SQLAlchemy stores Python enum *names*, not values.)

Purely additive — no existing data touched. Idempotent: CREATE TABLE/INDEX IF
NOT EXISTS + ALTER TYPE ADD VALUE IF NOT EXISTS; safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted live;
applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261001_run_completion_webhooks.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

CREATE_SUBSCRIPTIONS_SQL = """
CREATE TABLE IF NOT EXISTS run_webhook_subscriptions (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    workflow_id     INTEGER NOT NULL REFERENCES workflows(id),
    url             VARCHAR(2048) NOT NULL,
    secret_encrypted TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    description     TEXT,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);
"""

CREATE_DELIVERIES_SQL = """
CREATE TABLE IF NOT EXISTS run_webhook_deliveries (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    workflow_id     INTEGER REFERENCES workflows(id),
    subscription_id INTEGER REFERENCES run_webhook_subscriptions(id) ON DELETE SET NULL,
    run_id          INTEGER REFERENCES workflow_runs(id),
    url             VARCHAR(2048) NOT NULL,
    event           VARCHAR(50) NOT NULL,
    payload_body    TEXT NOT NULL,
    delivery_uuid   VARCHAR(36) NOT NULL UNIQUE,
    secret_encrypted TEXT,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempted_at TIMESTAMPTZ,
    last_http_status INTEGER,
    last_error      TEXT,
    next_attempt_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CREATE_ATTEMPTS_SQL = """
CREATE TABLE IF NOT EXISTS run_webhook_delivery_attempts (
    id              SERIAL PRIMARY KEY,
    delivery_id     INTEGER NOT NULL REFERENCES run_webhook_deliveries(id) ON DELETE CASCADE,
    attempt_number  INTEGER NOT NULL,
    outcome         VARCHAR(20) NOT NULL,
    http_status     INTEGER,
    error           TEXT,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

INDEX_DELIVERIES_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_run_webhook_deliveries_tenant_id
    ON run_webhook_deliveries (tenant_id);
"""
INDEX_DELIVERIES_STATE_SQL = """
CREATE INDEX IF NOT EXISTS ix_run_webhook_deliveries_state
    ON run_webhook_deliveries (status, next_attempt_at);
"""
INDEX_DELIVERIES_RUN_SQL = """
CREATE INDEX IF NOT EXISTS ix_run_webhook_deliveries_run_id
    ON run_webhook_deliveries (run_id);
"""
INDEX_ATTEMPTS_DELIVERY_SQL = """
CREATE INDEX IF NOT EXISTS ix_run_webhook_delivery_attempts_delivery_id
    ON run_webhook_delivery_attempts (delivery_id);
"""

AUDIT_VALUES = [
    "RUN_WEBHOOK_SUBSCRIPTION_CREATED",
    "RUN_WEBHOOK_SUBSCRIPTION_DELETED",
    "RUN_WEBHOOK_SECRET_ROTATED",
]

_STATEMENTS = [
    ("create run_webhook_subscriptions", CREATE_SUBSCRIPTIONS_SQL),
    ("create run_webhook_deliveries", CREATE_DELIVERIES_SQL),
    ("create run_webhook_delivery_attempts", CREATE_ATTEMPTS_SQL),
    ("index run_webhook_deliveries.tenant_id", INDEX_DELIVERIES_TENANT_SQL),
    ("index run_webhook_deliveries state", INDEX_DELIVERIES_STATE_SQL),
    ("index run_webhook_deliveries.run_id", INDEX_DELIVERIES_RUN_SQL),
    ("index run_webhook_delivery_attempts.delivery_id", INDEX_ATTEMPTS_DELIVERY_SQL),
]
_TABLES = [
    "run_webhook_subscriptions",
    "run_webhook_deliveries",
    "run_webhook_delivery_attempts",
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


def _existing_enum_values(cur) -> list:
    cur.execute(
        """
        SELECT e.enumlabel
          FROM pg_type t
          JOIN pg_enum e ON t.oid = e.enumtypid
         WHERE t.typname = 'auditaction'
         ORDER BY e.enumsortorder
        """
    )
    return [r[0] for r in cur.fetchall()]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"-- {label}")
            print(sql)
        for v in AUDIT_VALUES:
            print(f"-- ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{v}'")
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)

    try:
        for label, sql in _STATEMENTS:
            cur.execute(sql)
            print(f"WRITE  {label}")

        current = _existing_enum_values(cur)
        for v in AUDIT_VALUES:
            if v in current:
                continue
            # ALTER TYPE ... ADD VALUE must run outside a transaction block;
            # the connection is autocommit, so this is safe.
            cur.execute(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{v}'")
            print(f"WRITE  auditaction.{v}")
    finally:
        if set_role_ok:
            try:
                for t in _TABLES:
                    cur.execute(f'ALTER TABLE {t} OWNER TO {owner}')
                cur.execute("RESET ROLE")
            except Exception as e:  # noqa: BLE001
                print(f"(ALTER OWNER / RESET ROLE failed: {e})")
        cur.close()

    print("run_webhook_* tables + auditaction values ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
