"""Community Edition has no quantity limits: set every plan to unlimited.

Earlier CE builds seeded quantity rows with small values (1 seat / 5 GB / 1 concurrent run / 100
credits), and the boot heal capped internal rows at 1000 concurrent runs.
Nothing in CE enforces those columns any more, but a few readers still show or use
them (the capacity-queue dispatcher, the burst wait estimate, the /me seat figure), so
an upgraded install must not keep a row that says otherwise. The API seed now writes
the unlimited values itself; this migration brings rows an older build already wrote
to the same values.

Only the quantity columns that could cap an install are touched (seats, storage GB,
concurrent processes, credits per month); CPU-hours is already 0 = unlimited. Rows
already at or above the unlimited value are left alone, so the step is idempotent and
safe to re-run. It ships only in the Community Edition tree.

DRY-RUN by default. mod-migrate passes `--commit`:
    python3 migrations/20261109c_ce_plans_unlimited.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

UNLIMITED = 1_000_000_000_000_000

COLUMNS = ("max_seats", "max_storage_gb", "max_concurrent_processes", "credits_per_month")

_BELOW = " OR ".join(f"{c} < %s" for c in COLUMNS)
_SET = ", ".join(f"{c} = GREATEST({c}, %s)" for c in COLUMNS)


def make_plans_unlimited(cur, apply: bool) -> int:
    """Raise every plan's quantity columns to UNLIMITED on a DBAPI cursor, inside the
    caller's transaction. Returns the number of plan rows changed (or that would be)."""
    cur.execute("SELECT to_regclass('plans')")
    if cur.fetchone()[0] is None:
        print("plans: table not present yet, nothing to do.")
        return 0
    params = (UNLIMITED,) * len(COLUMNS)
    if not apply:
        cur.execute(f"SELECT count(*) FROM plans WHERE {_BELOW}", params)
        n = int(cur.fetchone()[0])
        print(f"plans: would set unlimited quantity columns on {n} row(s).")
        return n
    cur.execute(f"UPDATE plans SET {_SET} WHERE {_BELOW}", params + params)
    n = cur.rowcount
    print(f"plans: set unlimited quantity columns on {n} row(s).")
    return n


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
    dbapi.autocommit = False
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    conn = _get_conn()
    cur = conn.cursor()
    try:
        make_plans_unlimited(cur, apply=args.commit)
        if args.commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    if not args.commit:
        print("(dry-run; pass --commit to apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
