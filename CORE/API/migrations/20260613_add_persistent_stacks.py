"""add-persistent-stacks §1 — persistent_stacks table

Additive and inert until a stack is created. Idempotent (CREATE TABLE /
CREATE INDEX IF NOT EXISTS).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260613_add_persistent_stacks.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS persistent_stacks (
    id            SERIAL PRIMARY KEY,
    tenant_id     INTEGER NOT NULL REFERENCES tenants(id),
    name          VARCHAR(255) NOT NULL,
    compose_spec  JSONB NOT NULL,
    storage_class VARCHAR(20)  NOT NULL DEFAULT 'local_fs',
    storage_refs  JSONB,
    target_worker VARCHAR(255),
    home_zone     VARCHAR(100),
    status        VARCHAR(20)  NOT NULL DEFAULT 'created',
    lb_endpoint   VARCHAR(512),
    quota_bytes   BIGINT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_persistent_stacks_tenant_id     ON persistent_stacks (tenant_id);
CREATE INDEX IF NOT EXISTS ix_persistent_stacks_target_worker ON persistent_stacks (target_worker);
CREATE INDEX IF NOT EXISTS ix_persistent_stacks_home_zone     ON persistent_stacks (home_zone);
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        print(CREATE_TABLE_SQL)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(CREATE_TABLE_SQL)
    cur.close()
    print("persistent_stacks table ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
