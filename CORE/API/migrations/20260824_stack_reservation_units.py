"""persistent-stack-billing §2.1 — the per-stack reservation metering columns.

Adds two additive columns to `persistent_stacks`:
  - reservation_units   (JSONB, nullable) — the priced reservation vector
        {cpu_cores, mem_gb, gpu:[vram_gb...]}, snapshotted at deploy from
        _aggregate_accelerator_demand (GPU) + compose deploy.resources.reservations
        (cpu/mem). Null = nothing captured yet; priced as 0.
  - metered_through_at  (timestamptz, nullable) — the anchor for idempotent
        metering. Seeded to now() at deploy/wake, cleared on stop. Null = not
        currently metered; the sweep skips it.

Both nullable, so every existing stack is legal untouched. Dark by default: with
the reservation rates 0 and the flag off (see 20260824_reservation_rate_settings),
the tick captures units and moves zero credits. Run inside the API container:

    docker exec my_fastapi_app python3 migrations/20260824_stack_reservation_units.py --commit

Idempotent — existence is tested before each ALTER (the Vault dynamic credential
means a later run is a different Postgres role, so IF NOT EXISTS is unreliable).
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


_COLUMNS = [
    ("reservation_units", "JSONB"),
    ("metered_through_at", "TIMESTAMPTZ"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if not _table_exists(cur, "persistent_stacks"):
        print("Table persistent_stacks absent — refusing to run.")
        return 1

    for column, ddl in _COLUMNS:
        if _column_exists(cur, "persistent_stacks", column):
            print(f"Column persistent_stacks.{column} already present.")
        elif not args.commit:
            print(f"DRY RUN: would add persistent_stacks.{column} ({ddl})")
        else:
            cur.execute(f"ALTER TABLE persistent_stacks ADD COLUMN {column} {ddl}")
            print(f"Added persistent_stacks.{column}.")

    if not args.commit:
        print("\nDry run only. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
