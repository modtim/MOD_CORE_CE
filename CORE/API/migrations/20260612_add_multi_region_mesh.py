"""add-multi-region-mesh §1 — managers registry + ApiTaskQueue ownership columns

Additive and inert on a single-manager deployment:
  - the `managers` table starts empty (no peers enrolled),
  - `api_task_queue.owner_manager_id` stays NULL (no manager owns anything until
    the mesh is active),
  - `api_task_queue.fencing_token` defaults to 0.
No existing code path reads these columns yet, so behavior is unchanged.

Idempotent: a second run is a no-op (IF NOT EXISTS everywhere).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260612_add_multi_region_mesh.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_MANAGERS_SQL = """
CREATE TABLE IF NOT EXISTS managers (
    manager_id    VARCHAR(255) PRIMARY KEY,
    region        VARCHAR(100),
    zone          VARCHAR(100),
    url           VARCHAR(512) NOT NULL,
    status        VARCHAR(20)  NOT NULL DEFAULT 'active',
    last_seen     TIMESTAMPTZ,
    load_snapshot JSONB,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_managers_region ON managers (region);
CREATE INDEX IF NOT EXISTS ix_managers_status ON managers (status);
"""

ADD_OWNERSHIP_SQL = """
ALTER TABLE api_task_queue
    ADD COLUMN IF NOT EXISTS owner_manager_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS fencing_token    INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_api_task_queue_owner_manager_id
    ON api_task_queue (owner_manager_id);
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
        print(CREATE_MANAGERS_SQL)
        print(ADD_OWNERSHIP_SQL)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(CREATE_MANAGERS_SQL)
    cur.execute(ADD_OWNERSHIP_SQL)
    cur.close()
    print("managers table + api_task_queue.owner_manager_id/fencing_token ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
