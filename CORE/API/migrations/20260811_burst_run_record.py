"""burst-to-saas slice 4 — the execution record on every workflow run.

Adds to ``workflow_runs``:

  ran_remotely                BOOLEAN NOT NULL DEFAULT false
  burst_remote_run_id         VARCHAR(128)   -- the run id on MOD
  burst_submitted_at          TIMESTAMPTZ
  burst_bytes_sent            BIGINT
  burst_amount_charged        NUMERIC(12,4)
  burst_remote_failure_reason TEXT
  burst_stay_local_reason     VARCHAR(48)    -- written for LOCAL runs

Why on the run and not a side table: the record has to survive burst being
switched off. A side table invites a cleanup job, and the export would then
have holes exactly where somebody is auditing.

Why local runs are recorded too: "why didn't this burst?" must be answerable
from the run record rather than by reading logs. Every existing run
backfills as ran_remotely=false, which is true of all of them.

This is the adoption gate (design D5). A customer with compliance obligations
cannot EVALUATE burst unless they can prove afterwards which jobs left.

Run inside the API container:

    docker exec my_fastapi_app python3 migrations/20260811_burst_run_record.py --commit

Idempotent. Existence is tested before each add because the Vault dynamic
credential means a later run is a different Postgres role.
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
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (table,))
    return cur.fetchone() is not None


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def _index_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (name,))
    return cur.fetchone() is not None


_COLUMNS = [
    ("ran_remotely", "BOOLEAN NOT NULL DEFAULT false"),
    ("burst_remote_run_id", "VARCHAR(128)"),
    ("burst_submitted_at", "TIMESTAMPTZ"),
    ("burst_bytes_sent", "BIGINT"),
    ("burst_amount_charged", "NUMERIC(12,4)"),
    ("burst_remote_failure_reason", "TEXT"),
    ("burst_stay_local_reason", "VARCHAR(48)"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if not _table_exists(cur, "workflow_runs"):
        print("Table workflow_runs absent — refusing to run.")
        return 1

    for name, ddl in _COLUMNS:
        if _column_exists(cur, "workflow_runs", name):
            print(f"Column workflow_runs.{name} already present.")
            continue
        if not args.commit:
            print(f"DRY RUN: would add workflow_runs.{name}")
            continue
        cur.execute(f"ALTER TABLE workflow_runs ADD COLUMN {name} {ddl}")
        print(f"Added workflow_runs.{name}.")

    # Partial index: history filters on "show me what left", and remote runs
    # are the small minority.
    idx = "ix_workflow_runs_ran_remotely"
    if _index_exists(cur, idx):
        print(f"Index {idx} already present.")
    elif not args.commit:
        print(f"DRY RUN: would create index {idx}")
    else:
        cur.execute(
            f"CREATE INDEX {idx} ON workflow_runs (tenant_id, burst_submitted_at) "
            "WHERE ran_remotely"
        )
        print(f"Created index {idx}.")

    if args.commit:
        cur.execute("SELECT count(*) FROM workflow_runs WHERE ran_remotely")
        print(f"\nRuns recorded as remote: {cur.fetchone()[0]} "
              "(expected 0 immediately after this migration).")
    else:
        print("\nDry run only. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
