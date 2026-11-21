"""add-persistent-stacks §8.2 — stack_rebalancer_config (threshold dial) table

Singleton holding the admin-tunable rebalancer threshold. Additive; defaults to
0.10. Idempotent.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260615_add_stack_rebalancer_config.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stack_rebalancer_config (
    singleton_id       INTEGER PRIMARY KEY DEFAULT 1,
    threshold          DOUBLE PRECISION NOT NULL DEFAULT 0.10,
    updated_by_user_id INTEGER REFERENCES users(id),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_stack_rebalancer_config_singleton CHECK (singleton_id = 1),
    CONSTRAINT ck_stack_rebalancer_config_threshold_range CHECK (threshold >= 0 AND threshold <= 10)
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
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        print("DRY RUN — would execute:\n" + CREATE_TABLE_SQL)
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(CREATE_TABLE_SQL)
    cur.close()
    print("stack_rebalancer_config table ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
