"""managed-ai-surfaces-and-stack-ops §9.1/§9.3 — scheduled stack backups.

Backup existed only as `POST /persistent-stacks/{id}/backup`. A backup
capability that fires when someone remembers to press a button is a backup
capability that is absent on the day it is needed, and nothing in the platform
was taking a periodic copy of a stack's data.

WHY NULLABLE, AND WHY NULL IS NOT ZERO. `backup_interval_hours` NULL means "no
schedule has been set", which §9.2 requires to be distinguishable from a stack
someone deliberately decided needs no backups. Collapsing both into a disabled
flag loses the one that matters: a stack holding customer data that nobody has
made a decision about yet. Existing stacks all start NULL, which is honest —
none of them had a schedule, and the surface now says so out loud.

WHY THE FAILURE FIELDS ARE ON THE ROW. The on-demand endpoint reports a failure
to the operator who pressed the button. A scheduled backup has no such caller,
so its only trace was a log line in whichever uvicorn worker happened to hold
the scheduler lock (1 of 20 here) — unreachable over HTTP and effectively
invisible. §9.3 requires the failure to be reported against the stack.

Snapshot VERIFICATION state (§9.5) is deliberately NOT here: it belongs per
snapshot, and snapshots already live in `storage_refs["snapshots"][id]`. Adding
a per-stack verification column would describe the wrong thing — the stack's
recoverability is a property of each individual snapshot, not of the stack.

Additive + idempotent. Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260804_add_stack_backup_schedule.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


SQL = """
ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS backup_interval_hours INTEGER;

ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS backup_last_run_at TIMESTAMPTZ;

ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS backup_last_error TEXT;

ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS backup_last_error_at TIMESTAMPTZ;

-- The scheduler sweep asks "which stacks are due", so it filters on the
-- schedule being set at all. Partial: the majority of rows are NULL and there
-- is no reason to carry them in the index.
CREATE INDEX IF NOT EXISTS ix_persistent_stacks_backup_due
    ON persistent_stacks(backup_interval_hours, backup_last_run_at)
    WHERE backup_interval_hours IS NOT NULL;
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
    print("backup schedule ready. Every existing stack has NO schedule "
          "(backup_interval_hours IS NULL) — which is what was already true, "
          "now stated rather than assumed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
