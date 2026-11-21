"""tailnet-plan task 7.9 — account-service DERP verify: relay entitlement + node map.

Backs the verify-client-URL admission callback MOD's DERP calls for each connecting
node. Adds:
  - plans.relay_entitled           — does this plan include MOD DERP relay
  - relay_authorized_node          — node public key -> billing tenant, so the
                                     callback can resolve a connecting node to an
                                     account (mod-hosted enrollment or a pasted
                                     relay-token registration for customer-owned)

Additive + idempotent. Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260901_add_relay_verify.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


COLUMNS_SQL = """
ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS relay_entitled BOOLEAN NOT NULL DEFAULT false;
"""

NODE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS relay_authorized_node (
    node_key    VARCHAR(255) PRIMARY KEY,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source      VARCHAR(32) NOT NULL DEFAULT 'mod-hosted',  -- mod-hosted | relay-token
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_relay_authorized_node_tenant_id
    ON relay_authorized_node(tenant_id);
"""

STATEMENTS = [COLUMNS_SQL, NODE_TABLE_SQL]


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
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        for s in STATEMENTS:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    for s in STATEMENTS:
        cur.execute(s)
    cur.close()
    print("relay-verify schema ready: plans.relay_entitled, relay_authorized_node.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
