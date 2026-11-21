"""Calibration coordinator — plan/progress table.

Replaces the fire-and-forget batch_calibrator.run_batch_calibration model
(enqueue all 42k tasks at once and rely on priority + capacity floor)
with a stateful coordinator that drips work into the queue at a bounded
rate. One row per active plan; the APScheduler tick reads it, computes
how many calibration tasks are currently in flight, and enqueues the
next NodeDef's matrix only when in-flight is below target.

Schema:
- id (PK)
- tenant_id (FK) — initiator tenant; in practice the platform tenant
- status — running | paused | completed | cancelled
- target_in_flight (INT) — max in-flight calibration tasks (CLAIMED +
  RUNNING + ASSIGNED). When current >= target, tick defers.
- total_nodes (INT) — frozen at plan creation (count of eligible
  NodeDefinitions). Lets the UI render N/Total progress.
- nodes_completed (INT) — count of NodeDefs whose 84-task matrix has
  fully terminated and been swept to calibration_status='calibrated'.
- nodes_failed (INT) — count of NodeDefs whose matrix had a failure
  rate above threshold (placeholder; sweep treats any per-node
  outcome as 'attempted', failures noted only).
- nodes_in_progress (JSONB) — array of node_definition_id integers
  currently being calibrated (their 84 tasks are en route or running).
- last_tick_at, last_tick_reason — diagnostic state.
- created_at, updated_at — timestamps.

A partial UNIQUE index on status='running' enforces "at most one active
plan per tenant" so the coordinator doesn't need ambiguity-resolution.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260507_calibration_plans_table.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    steps: list[tuple[str, str]] = []

    if not _table_exists(cur, "calibration_plans"):
        steps.append((
            "create calibration_plans table",
            """
            CREATE TABLE calibration_plans (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                status VARCHAR(20) NOT NULL DEFAULT 'running',
                target_in_flight INTEGER NOT NULL DEFAULT 30,
                total_nodes INTEGER NOT NULL,
                nodes_completed INTEGER NOT NULL DEFAULT 0,
                nodes_failed INTEGER NOT NULL DEFAULT 0,
                nodes_in_progress JSONB NOT NULL DEFAULT '[]'::jsonb,
                last_tick_at TIMESTAMPTZ,
                last_tick_reason TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
        ))
        steps.append((
            "index tenant_id",
            "CREATE INDEX IF NOT EXISTS ix_calibration_plans_tenant ON calibration_plans(tenant_id)",
        ))
        steps.append((
            "partial unique index — at most one running plan per tenant",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_calibration_plans_running_per_tenant
                ON calibration_plans(tenant_id) WHERE status = 'running'
            """,
        ))
        steps.append((
            "index status for sweep queries",
            "CREATE INDEX IF NOT EXISTS ix_calibration_plans_status ON calibration_plans(status)",
        ))

    if not steps:
        print("schema already current — nothing to do.")
        return 0

    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        print(f"        {stmt.strip()[:200]}")
        if args.commit:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
