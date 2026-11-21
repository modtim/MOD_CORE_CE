#!/usr/bin/env python3
"""sz-1.1.1 (sz-productivity-floor decomp, §1.1) — the ONE new table:
NODE COST BENCHMARKS (per node-definition / size-bucket observed
cost+runtime stats).

revision      : 20261010c_node_cost_benchmarks
down_revision : 20261010b_run_price_quotes

A future cost forecaster (sz-1.4.1, NOT this task) reads this table to
turn "this node, at this input size bucket" into a predicted runtime and
credit cost. This migration only creates the storage; no writer/reader
code is wired here.

Columns, per the sz-1.1.1 decomp row:
    id                  BIGSERIAL PRIMARY KEY
    node_definition_id  INTEGER REFERENCES node_definitions (id)
                        ON DELETE SET NULL (nullable) — a benchmark row for
                        a node definition that's since been soft-pruned is
                        still useful historical data, matching the
                        no-hard-delete ISO posture; it just stops pointing
                        at a live node_definitions row.
    size_bucket         VARCHAR(40) NOT NULL — free-text format-family
                        bucket name ("4MP", "32MP", "300s", ...); the
                        forecaster (sz-1.4.1) owns bucket naming, no enum
                        here.
    sample_count        INTEGER NOT NULL DEFAULT 0
    p50_runtime_ms      INTEGER
    p95_runtime_ms      INTEGER
    p50_cost_credits    INTEGER
    p95_cost_credits    INTEGER
    last_observed_at    TIMESTAMPTZ
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()

Unique constraint on (node_definition_id, size_bucket): one row per
node/bucket, updated in place as more samples arrive.

Indexes: ix_node_cost_benchmarks_node on (node_definition_id).

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. No models.py ORM
class is added by this migration — table existence + columns are what
sz-1.4.1 depends on, verified by plain SQL introspection.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261010c_node_cost_benchmarks.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "node_cost_benchmarks"

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-1.1.1, per node-definition/size-bucket "
        "cost+runtime benchmarks)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    node_definition_id INTEGER REFERENCES node_definitions (id)
        ON DELETE SET NULL,
    size_bucket VARCHAR(40) NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    p50_runtime_ms INTEGER,
    p95_runtime_ms INTEGER,
    p50_cost_credits INTEGER,
    p95_cost_credits INTEGER,
    last_observed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_{_TABLE}_node_bucket UNIQUE (node_definition_id, size_bucket)
);
""",
    ),
    (
        f"{_TABLE} index (node_definition_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_node
    ON {_TABLE} (node_definition_id);
""",
    ),
]


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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the CREATE TABLE + index steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (benchmark rows are kept as "
              "historical data even after their node definition is "
              "soft-pruned)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        try:
            cur.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "sz-1.1.1 ready: node_cost_benchmarks exists; "
        "sz-1.4.1 forecaster can read per-(node_definition, size_bucket) "
        "benchmarks."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
