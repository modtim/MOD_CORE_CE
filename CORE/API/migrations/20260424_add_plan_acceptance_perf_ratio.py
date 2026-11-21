"""Phase 11 — add plans.acceptance_perf_ratio column.

Per-plan perf-gate acceptance threshold. The Phase 11 smoke_probe
perf capture computes `cpu_p50 / variant_p50`; the API uses
`Plan.acceptance_perf_ratio` to decide whether the variant flips to
`verified` (accepted) or `perf_too_slow` (below threshold).

Null on existing rows — the API's `port_acceptance.evaluate_acceptance`
falls back to its module default (0.25 = variant must be at least 25%
as fast as CPU) when the column is null.

Idempotent — a second run is a no-op.

Run:
  docker exec my_fastapi_app python3 migrations/20260424_add_plan_acceptance_perf_ratio.py --commit
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
    dbapi = getattr(raw, "driver_connection", None) or \
        getattr(raw, "connection", None) or raw
    dbapi.autocommit = True
    return dbapi


def _column_exists(cur, table: str, col: str) -> bool:
    cur.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
         LIMIT 1
        """,
        (table, col),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()
    if _column_exists(cur, "plans", "acceptance_perf_ratio"):
        print("plans.acceptance_perf_ratio already present — nothing to do.")
        return 0

    stmt = (
        "ALTER TABLE plans "
        "ADD COLUMN IF NOT EXISTS acceptance_perf_ratio double precision NULL"
    )
    print(f"{'WRITE' if args.commit else 'DRY  '}  {stmt}")
    if args.commit:
        cur.execute(stmt)
        if not _column_exists(cur, "plans", "acceptance_perf_ratio"):
            print("ERROR: column not present post-ALTER", file=sys.stderr)
            return 1
        print("Applied.")
    else:
        print("(dry run — re-run with --commit to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
