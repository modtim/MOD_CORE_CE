"""27.6.s2 (launch-completion-megaplan, gr-llm-serving) — stack_bench_profiles table.

Adds `stack_bench_profiles`, the append-only ledger of `bench` stack-op results
(the worker's capacity sweep, Worker/internal/executor/stack_bench.go, reports
TTFT per prompt size + decode tok/s per concurrency + cache-hit TTFT in a
MOD_STACK_BENCH sentinel block on the task log tail).

27.6 says those results go to stack_profiles and feed the admission limits in
27.4. StackProfile is one-row-per-stack and gets FOLDED by the calibration
path — a bench measurement must never overwrite an older one (ISO 27001: no
hard deletes, measurements are provenance), so the bench gets its own append-
only row keyed to (stack, revision_no, worker): capacity is a property of the
composition AND the machine that served it, and readers take the newest.

Purely additive — no existing data touched. Idempotent (CREATE TABLE IF NOT
EXISTS / CREATE ... IF NOT EXISTS); safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted live;
applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \
        migrations/20261008_stack_bench_profiles.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys


_TABLE = "stack_bench_profiles"

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS stack_bench_profiles (
    id BIGSERIAL PRIMARY KEY,
    stack_id BIGINT NULL REFERENCES persistent_stacks (id) ON DELETE SET NULL,
    stack_name VARCHAR(255) NULL,
    revision_no INTEGER NOT NULL DEFAULT 0,
    worker_name VARCHAR(255) NULL,
    model VARCHAR(255) NULL,
    report JSONB NULL,
    errors JSONB NULL,
    benched_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_stack_bench_profiles_stack
    ON stack_bench_profiles (stack_id);
"""

STATEMENTS = [
    ("create stack_bench_profiles (FK inline: ON DELETE SET NULL)", CREATE_SQL),
    ("index stack_bench_profiles.stack_id", INDEX_SQL),
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
        for label, sql in STATEMENTS:
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        try:
            cur.execute(f'ALTER TABLE {_TABLE} OWNER TO {owner}')
        except Exception as e:  # noqa: BLE001
            print(f"(ALTER TABLE {_TABLE} OWNER TO {owner} failed: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("stack_bench_profiles ready (append-only bench ledger).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
