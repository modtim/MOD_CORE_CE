"""Phase 2 — create the tenant_slack_configs table.

Each tenant gets one row (tenant_id is the PK) with:
- webhook_url_secret_id (FK → secrets) — the encrypted Slack webhook URL
- enabled flag + default_channel + subscribed_events JSON list
- last_test_at / last_test_status for the "send a test ping" UX

Idempotent: a second run is a no-op.

Run inside the API container:
    docker exec my_fastapi_app python3 migrations/20260425_add_tenant_slack_configs.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS tenant_slack_configs (
    tenant_id INTEGER NOT NULL PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    webhook_url_secret_id INTEGER REFERENCES secrets(id) ON DELETE SET NULL,
    default_channel VARCHAR(80),
    enabled BOOLEAN NOT NULL DEFAULT false,
    subscribed_events JSON NOT NULL DEFAULT '[]'::json,
    last_test_at TIMESTAMP WITH TIME ZONE,
    last_test_status VARCHAR(32),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
"""


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


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name=%s LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                    help="Apply the CREATE. Without this flag, dry-run only.")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if _table_exists(cur, "tenant_slack_configs"):
        print("tenant_slack_configs already exists — nothing to do.")
        return 0

    print(f"{'WRITE' if args.commit else 'DRY  '}  CREATE TABLE tenant_slack_configs ...")
    if args.commit:
        cur.execute(CREATE_SQL)
        if not _table_exists(cur, "tenant_slack_configs"):
            print("ERROR: table not present after CREATE", file=sys.stderr)
            return 1
        print("Applied.")
    else:
        print("(dry run — re-run with --commit to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
