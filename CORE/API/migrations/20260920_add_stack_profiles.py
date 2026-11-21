"""stack-training-and-prewarm — StackProfile table (Gate 0, task 1.1).

Adds `stack_profiles`, one row per managed stack: the measured sibling of
`accelerator_profiles`. It holds the rolling warm-time p90, idle/serving VRAM
footprint, readiness-probe validity, and the correctness smoke-test outcome that
the fit-gate and pre-warm planner read (see design.md — StackProfile decision).

Keyed by stack_name (managed stacks are platform-owned singletons every wake path
already identifies by name), with an optional stack_id back-reference to
persistent_stacks. The warm-time p90 is recomputed from a bounded rolling window
kept in warm_samples; `samples` is the lifetime observation count.

Additive + idempotent (CREATE TABLE IF NOT EXISTS + ADD COLUMN IF NOT EXISTS), so
a deployment that never runs it behaves exactly as before (the model tolerates the
table's absence only in tests). APPLY THIS before deploying the code.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260920_add_stack_profiles.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stack_profiles (
    id              SERIAL PRIMARY KEY,
    stack_name      VARCHAR(255) NOT NULL,
    stack_id        INTEGER REFERENCES persistent_stacks(id),
    warm_time_p90_s DOUBLE PRECISION,
    idle_vram_gb    DOUBLE PRECISION,
    serving_vram_gb DOUBLE PRECISION,
    probe_valid     BOOLEAN,
    test_status     VARCHAR(16) NOT NULL DEFAULT 'unknown',
    test_reason     TEXT,
    last_tested_at  TIMESTAMPTZ,
    samples         INTEGER NOT NULL DEFAULT 0,
    warm_samples    JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Idempotent guards so a table that predates a column (or a re-run) converges.
ADD_COLUMNS_SQL = """
ALTER TABLE stack_profiles
    ADD COLUMN IF NOT EXISTS stack_id        INTEGER REFERENCES persistent_stacks(id),
    ADD COLUMN IF NOT EXISTS warm_time_p90_s DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS idle_vram_gb    DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS serving_vram_gb DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS probe_valid     BOOLEAN,
    ADD COLUMN IF NOT EXISTS test_status     VARCHAR(16) NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS test_reason     TEXT,
    ADD COLUMN IF NOT EXISTS last_tested_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS samples         INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS warm_samples    JSONB,
    ADD COLUMN IF NOT EXISTS created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at      TIMESTAMPTZ NOT NULL DEFAULT now();
"""

# One profile per managed stack — enforced by a unique index on the name.
UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_stack_profiles_stack_name
    ON stack_profiles (stack_name);
"""

STACK_ID_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_stack_profiles_stack_id
    ON stack_profiles (stack_id);
"""

STATEMENTS = [CREATE_TABLE_SQL, ADD_COLUMNS_SQL, UNIQUE_INDEX_SQL, STACK_ID_INDEX_SQL]


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
    print("stack_profiles ready (one row per managed stack).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
