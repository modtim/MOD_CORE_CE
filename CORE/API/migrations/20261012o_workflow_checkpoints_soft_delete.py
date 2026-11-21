#!/usr/bin/env python3
"""expr 9.6 (workflow-expressiveness-primitives decomp, §9.6) -- SOFT-DELETE
TOMBSTONE + live index for WORKFLOW CHECKPOINTS (daily TTL sweep, default 30d).

revision      : 20261012o_workflow_checkpoints_soft_delete
down_revision : 20261012n_workflow_parameter_versions
                (task 585 is writing that migration RIGHT NOW as
                 migrations/20261012n_workflow_parameter_versions.py; apply n
                 before o.)

Purpose: the ISO 27001 owner rule says MOD NEVER hard-deletes -- everything is
soft-deleted and audited. ``workflow_checkpoints`` (models.WorkflowCheckpoint,
models.py ~3676: id, workflow_run_id, node_run_id, attempt, label, progress,
payload JSONB, completed_at) carries NO ``deleted_at`` column today, so the
daily retention sweep (services/workflow_checkpoint_retention.py, expr 9.6)
has nowhere to stamp a tombstone. This migration lands exactly that storage.
No writer/reader code is wired here -- same posture as its sibling migrations
(storage first, service later).

Columns:
    workflow_checkpoints
        deleted_at  TIMESTAMPTZ NULL -- soft delete only (ISO 27001). NULL =
                    live. The sweep stamps it on checkpoints whose completed_at
                    passed their TTL AND whose workflow_run is already
                    terminal; nothing is ever removed from the table.

Indexes:
    ix_workflow_checkpoints_live_completed PARTIAL on (completed_at) WHERE
        deleted_at IS NULL -- the sweep's own shape: "give me the next
        :batch live checkpoints older than the cutoff". The partial index
        covers only live rows, so a table that has mostly aged into
        tombstones keeps the scan cheap instead of re-walking every dead
        row every tick.

NO `ON DELETE CASCADE` is introduced or changed: the table's existing FKs to
workflow_runs / node_runs already carry ondelete=CASCADE in models.py (a
checkpoint falls out alongside the run that made it) and this migration does
not touch them -- it only adds a column and an index.

Idempotent: ALTER TABLE ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS;
safe to re-run. Additive only: no drops, no re-placed columns, no data changes
(models.py already carries this table, so the ALTER is the only statement that
can ever do work here).

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012o_workflow_checkpoints_soft_delete.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "workflow_checkpoints"

_STATEMENTS = [
    (
        f"{_TABLE} deleted_at tombstone (expr 9.6, soft delete only -- the "
        "table has no deleted_at today, so the TTL sweep has nowhere to stamp)",
        f"""
ALTER TABLE {_TABLE}
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
""",
    ),
    (
        f"{_TABLE} PARTIAL index (completed_at) WHERE deleted_at IS NULL "
        "-- the sweep's scan: next :batch live checkpoints past the cutoff",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_live_completed
    ON {_TABLE} (completed_at)
    WHERE deleted_at IS NULL;
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
                   help="Apply the ALTER + index steps. Without this flag, "
                        "dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (checkpoints are kept as historical run "
              "data; the table is append-only with soft deletes only, so "
              "nothing is dropped on rollback)")
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
        "expr 9.6 ready: workflow_checkpoints carries deleted_at + the live "
        "partial index; the daily TTL sweep (services/workflow_checkpoint_"
        "retention.py) and the quota exclusion can read/write it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
