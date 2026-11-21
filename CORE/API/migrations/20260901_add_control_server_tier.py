"""tailnet-plan Groups 1 + 7 — install-level control-server tier + DERP relay owner.

A single platform-admin-tunable posture for WHERE this install's mesh control
server lives (disabled / mod-hosted / customer-owned) and WHO operates the DERP
relay (mod-derp / customer-derp / direct-only), plus the external coordinator
location for the customer-owned tier. Singleton (id=1), same shape as
mesh_replication_config / scheduler_runtime_config.

ADDITIVE + INERT: every column is nullable and NULL means "derive from today's
env reality" (services/control_server_tier.resolve_*), so a deploy changes
nothing until an admin sets a value.

Additive + idempotent. Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260901_add_control_server_tier.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


TABLE_SQL = """
CREATE TABLE IF NOT EXISTS control_server_config (
    singleton_id         INTEGER PRIMARY KEY DEFAULT 1,
    control_server_tier  VARCHAR(32),
    relay_owner          VARCHAR(32),
    coordinator_location VARCHAR(512),
    updated_by_user_id   INTEGER REFERENCES users(id),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    audit_event_id       BIGINT REFERENCES audit_events(id),
    CONSTRAINT ck_control_server_config_singleton CHECK (singleton_id = 1),
    CONSTRAINT ck_control_server_tier CHECK (
        control_server_tier IS NULL
        OR control_server_tier IN ('disabled','mod-hosted','customer-owned')),
    CONSTRAINT ck_control_server_relay_owner CHECK (
        relay_owner IS NULL
        OR relay_owner IN ('mod-derp','customer-derp','direct-only'))
);
"""

STATEMENTS = [TABLE_SQL]


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
    print("control-server-tier schema ready: control_server_config "
          "(control_server_tier, relay_owner, coordinator_location).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
