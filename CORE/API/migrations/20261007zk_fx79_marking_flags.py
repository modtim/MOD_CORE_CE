"""GSEC FX.79 part 2b — the ``blocks_shared_gpu`` marking-label flag.

Part 1a created ``marking_label_defs`` with ``requires_us_person`` /
``blocks_external_ai`` / ``blocks_external_share``. Part 2b adds ONE more
flag so a customer label can additionally say "marked items may not run on
non-tenant (shared / harvested / marketplace) GPUs":

    marking_label_defs.blocks_shared_gpu  BOOLEAN NULL DEFAULT FALSE
        NULL-safe: pre-existing rows read back as NULL, and the code treats
        NULL/absent as False (bool(x or False)), so the column is additive
        and non-breaking. ``IF NOT EXISTS`` keeps re-runs a no-op on PG 9.6+
        ALTER TABLE ... ADD COLUMN (the runner's PG is well past 9.6).

Idempotent (ADD COLUMN IF NOT EXISTS; the UPDATE backfill is a no-op on a
second run). Dry run (default) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zk_fx79_marking_flags.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zk_fx79_marking_flags.py --commit   # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in this
directory, run in filename order) — this file sorts after
20261007ze_fx85_notice_subscriptions.py. NOT applied by this task (model /
migration / tests only) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "marking_label_defs.blocks_shared_gpu",
        "ALTER TABLE marking_label_defs "
        "ADD COLUMN IF NOT EXISTS blocks_shared_gpu BOOLEAN",
    ),
    (
        "marking_label_defs.blocks_shared_gpu default",
        "ALTER TABLE marking_label_defs "
        "ALTER COLUMN blocks_shared_gpu SET DEFAULT FALSE",
    ),
    # Backfill existing rows to an explicit FALSE (pre-existing rows are NULL
    # after the ADD COLUMN; readers already treat NULL as False, but an
    # explicit FALSE keeps the column non-null going forward and makes the
    # flag visible/true in the admin UI without a per-row touch).
    (
        "marking_label_defs.blocks_shared_gpu backfill",
        "UPDATE marking_label_defs "
        "SET blocks_shared_gpu = FALSE WHERE blocks_shared_gpu IS NULL",
    ),
]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

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
    conn = dbapi
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    try:
        for label, sql in _STATEMENTS:
            cur.execute(sql)
            print(f"WRITE  {label}")
    finally:
        if set_role_ok:
            cur.execute("RESET ROLE")
        cur.close()
    print("FX.79 p2b marking flag ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
