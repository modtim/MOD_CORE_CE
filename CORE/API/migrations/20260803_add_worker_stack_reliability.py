"""managed-ai-surfaces-and-stack-ops §8.6 — per-worker stack reliability history.

The rebalancer chose targets on FREE CAPACITY ALONE. That answers "which host
looks idle" and never "which host actually keeps a stack up", and the two come
apart in the worst way: a worker with a flaky card, a bad driver or a failing
PSU is idle PRECISELY BECAUSE its stacks keep dying and freeing capacity. Free
capacity therefore steers stacks toward the host least able to hold them.

WHY A NEW TABLE. `stack_observations` looks like the history and is not. It is
upserted one row per (worker_name, project) and the row is DELETED the moment
the project stops being reported (services/stack_observation.py). So it carries
current state: `observed_at` is always ~now, and a seven-day window returns
however many compose projects sit on that host at this instant — typically one
to three. A first cut of §8.6 read it as a time series and, because the sample
count never cleared its minimum, scored every worker as neutral and changed no
decision at all. Nothing else in the schema records per-host stack outcomes:
`worker_resource_timelines` is also one-row-per-worker-latest, and
`worker_assignment_history` tracks tenant assignment, not runtime.

SHAPE follows tenant_egress_usage_daily / tenant_harbor_usage_daily: one row per
(worker, day), counters accumulated by upsert, never rewritten. Daily buckets
keep the volume bounded — a heartbeat-rate table would write millions of rows to
answer a question that only needs a rate.

`stacks_observed` is the denominator and is not optional: three failures on a
host running thirty stacks and three on a host running three are the same
integer and opposite facts. It holds the day's PEAK stack count rather than a
running tally, so the window sum is stack-days of exposure. Accumulating it
per heartbeat would reach ~11,500 a day for two stacks at a 15s beat, and any
real failure rate divided by that rounds to zero.

Additive + idempotent. Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260803_add_worker_stack_reliability.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


# No FK to worker_licenses. History about a worker must outlive the worker row:
# a host that was removed BECAUSE it kept killing stacks is exactly the record
# worth keeping, and ON DELETE CASCADE would erase the evidence for the decision.
SQL = """
CREATE TABLE IF NOT EXISTS worker_stack_reliability_daily (
    id              SERIAL PRIMARY KEY,
    worker_name     VARCHAR(255) NOT NULL,
    day             DATE NOT NULL,
    down_events     INTEGER NOT NULL DEFAULT 0,
    deploy_failures INTEGER NOT NULL DEFAULT 0,
    stacks_observed INTEGER NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_stack_reliability_worker_day
    ON worker_stack_reliability_daily(worker_name, day);

CREATE INDEX IF NOT EXISTS ix_worker_stack_reliability_day
    ON worker_stack_reliability_daily(day);
"""

STATEMENTS = [SQL]


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
    print("worker_stack_reliability_daily ready. It starts EMPTY: until the "
          "window fills, the rebalancer falls back to free capacity and says "
          "so (history_used=false on every move).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
