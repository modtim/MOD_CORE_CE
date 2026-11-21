"""MKT.C2 — marketplace_webhook_events: idempotency ledger for the
Marketplace purchase-delivery webhook
(POST /v1/marketplace/purchase-webhook).

One row per ``sale_id``; the UNIQUE sale_id constraint is the webhook's
idempotency key — a replayed sale returns the original stored result and
changes no entitlements. ``response_body`` keeps the ORIGINAL delivery's
HTTP response so retries converge on one answer.

ISO 27001: rows are only ever status-updated (processed -> revoked), never
deleted.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS; safe to re-run.

NOT APPLIED YET — the lead applies it. When running:
    docker exec my_fastapi_app python3 \\
        migrations/20260928_marketplace_webhook_events.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS marketplace_webhook_events (
    id              BIGSERIAL PRIMARY KEY,
    sale_id         VARCHAR(255) NOT NULL,
    tenant_id       INTEGER REFERENCES tenants(id),
    response_body   JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          VARCHAR(20) NOT NULL DEFAULT 'processed',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_marketplace_webhook_event_sale_id
        UNIQUE (sale_id)
);
"""

INDEX_EVENTS_SALE_SQL = """
CREATE INDEX IF NOT EXISTS ix_marketplace_webhook_events_sale_id
    ON marketplace_webhook_events (sale_id);
"""

INDEX_EVENTS_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_marketplace_webhook_events_tenant
    ON marketplace_webhook_events (tenant_id);
"""

STATEMENTS = [
    CREATE_EVENTS_SQL,
    INDEX_EVENTS_SALE_SQL,
    INDEX_EVENTS_TENANT_SQL,
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
        for s in STATEMENTS:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    # Create the new objects owned by modTech (not the transient Vault dynamic
    # role that runs this migration) so ownership matches every other object
    # and the API's startup ALTER-OWNER reconcile stays a no-op. The app's
    # dynamic role is a member of modTech, so SET ROLE succeeds; fall back to
    # plain execution if it can't (dev/sqlite).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for s in STATEMENTS:
        cur.execute(s)
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("marketplace_webhook_events ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
