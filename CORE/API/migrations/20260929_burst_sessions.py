"""GH.4 — burst_sessions: the burst-hour meter, one row per burst worker's
lifetime (join -> leave), for launch-completion-megaplan group H
(decomp/gh-burst.md).

burst_sessions:
  tenant_id             owning tenant
  account_id            FK tenant_burst_accounts.id (nullable — teardown
                         routing; a session can still meter if the account
                         row is later soft-deleted)
  instance_id           cloud instance id/label (matches the
                         `mod-burst-instance` tag burst_bootstrap /
                         burst_reconciler use)
  worker_name           the enrolled worker name ("burst-<label>")
  gpu_class             key into burst_pricing.burst_fee_credits_per_hour
  status                'active' | 'draining' | 'closed'
  joined_at / left_at   the meter bracket
  last_debited_at       idempotency watermark (mirrors
                         PersistentStack.metered_through_at) — advanced in
                         the SAME transaction as the per-tick debit
  credits_debited_total running sum of what this session has cost so far

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS; safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260929_burst_sessions.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_BURST_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS burst_sessions (
    id                      SERIAL PRIMARY KEY,
    tenant_id               INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    account_id              INTEGER REFERENCES tenant_burst_accounts(id),
    instance_id             VARCHAR(255),
    worker_name             VARCHAR(255),
    gpu_class               VARCHAR(50) NOT NULL DEFAULT 'default',
    status                  VARCHAR(20) NOT NULL DEFAULT 'active',
    joined_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    left_at                 TIMESTAMPTZ,
    last_debited_at         TIMESTAMPTZ,
    credits_debited_total   BIGINT NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

INDEX_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_burst_sessions_tenant_id
    ON burst_sessions (tenant_id);
"""
INDEX_ACCOUNT_SQL = """
CREATE INDEX IF NOT EXISTS ix_burst_sessions_account_id
    ON burst_sessions (account_id);
"""
INDEX_INSTANCE_SQL = """
CREATE INDEX IF NOT EXISTS ix_burst_sessions_instance_id
    ON burst_sessions (instance_id);
"""
INDEX_STATUS_SQL = """
CREATE INDEX IF NOT EXISTS ix_burst_sessions_status
    ON burst_sessions (status);
"""

STATEMENTS = [
    CREATE_BURST_SESSIONS_SQL,
    INDEX_TENANT_SQL,
    INDEX_ACCOUNT_SQL,
    INDEX_INSTANCE_SQL,
    INDEX_STATUS_SQL,
]

TABLES = ["burst_sessions"]


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
                print(f"(ALTER TABLE {t} OWNER TO {owner} failed: {e})")
    print("Applied burst_sessions table + indexes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
