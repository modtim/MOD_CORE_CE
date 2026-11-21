"""add-workflow-triggers §1.4 — trigger_event_log table

Backs the trigger evaluation pipeline (dedup, rate-limit, debounce, composite/
fan-in arrival state). Additive and inert until a trigger fires. Idempotent.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260614_add_trigger_event_log.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trigger_event_log (
    id            BIGSERIAL PRIMARY KEY,
    trigger_id    INTEGER NOT NULL REFERENCES workflow_triggers(id) ON DELETE CASCADE,
    tenant_id     INTEGER REFERENCES tenants(id),
    event_key     VARCHAR(255) NOT NULL,
    fired_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    dedupe_until  TIMESTAMPTZ,
    state         JSONB
);
CREATE INDEX IF NOT EXISTS ix_trigger_event_log_trigger_id  ON trigger_event_log (trigger_id);
CREATE INDEX IF NOT EXISTS ix_trigger_event_log_tenant_id   ON trigger_event_log (tenant_id);
CREATE INDEX IF NOT EXISTS ix_trigger_event_log_event_key   ON trigger_event_log (event_key);
CREATE INDEX IF NOT EXISTS ix_trigger_event_log_fired_at    ON trigger_event_log (fired_at);
CREATE INDEX IF NOT EXISTS ix_trigger_event_log_trigger_key ON trigger_event_log (trigger_id, event_key);
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
        print("DRY RUN — would execute:\n" + CREATE_TABLE_SQL)
        print("Re-run with --commit to apply.")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(CREATE_TABLE_SQL)
    cur.close()
    print("trigger_event_log table ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
