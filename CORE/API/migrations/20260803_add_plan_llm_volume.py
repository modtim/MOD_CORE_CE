"""managed-ai-surfaces-and-stack-ops §2 — LLM call volume belongs to the plan.

The first cut of LLM rate limiting carried its own calls-per-window cap, which
duplicated `plans.api_rate_limit_per_minute` — an existing, live, plan-aware
sliding window (utils/rate_limiter). Two independent volume limiters with
separate configuration means an admin who gets refused has to work out WHICH one
refused them, and a plan upgrade raises one ceiling but not the other.

Volume is "what the customer bought", so it belongs on the plan, next to
credits_per_month and api_rate_limit_per_minute. Concurrency is a different
thing — it protects the shared model server from a burst regardless of what
anyone bought — and stays on the platform/tenant rows.

The granularity difference is why the existing column is not sufficient on its
own: `api_rate_limit_per_minute` counts HTTP REQUESTS, and one wizard request
fans out into many LLM calls (several critic passes). Bounding requests does not
bound LLM volume.

Adds to `plans`:
  - llm_calls_per_hour   — 0 means unlimited, matching api_rate_limit_per_minute
  - llm_concurrent_max   — NULL means inherit the platform default

Additive + idempotent. Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260803_add_plan_llm_volume.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


# 0 = unlimited, deliberately matching api_rate_limit_per_minute's convention
# so the two volume knobs on this table read the same way. Defaulting to 0
# means an upgrade changes nothing until an operator sets a number.
COLUMNS_SQL = """
ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS llm_calls_per_hour INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS llm_concurrent_max INTEGER;
"""

STATEMENTS = [COLUMNS_SQL]


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
    print("plans.llm_calls_per_hour (0 = unlimited) and plans.llm_concurrent_max "
          "(NULL = inherit platform) ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
