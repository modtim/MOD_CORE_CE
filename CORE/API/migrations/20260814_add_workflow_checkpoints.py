"""Gate 3 — a long run survives a crash.

A node five hours into a six-hour computation currently restarts from zero. Not
because the run is lost — the DAG advances event-driven on task completion, node
outputs land in MinIO BEFORE the report, and the reaper requeues an expired claim
within 30s, so a dead worker costs the interrupted NODE and not the run — but
because there is nowhere for partial progress to survive an attempt. Every task
gets an ephemeral /data volume that `defer cleanupVolume` removes, so attempt 2
starts blank.

Adds:
  - node_runs.checkpoint_volume  — the durable volume this node's attempts share
  - workflow_checkpoints         — what a node reported, when, and how far in

The volume is keyed to the NODE RUN, not the attempt, which is the whole point:
attempt 2 mounts what attempt 1 left. `node_output_cache` does not help here —
it is keyed on completed outputs, and a node killed at hour five completed
nothing to record.

Checkpointing is OPT-IN per node. A node that ignores MOD_CHECKPOINT_DIR behaves
exactly as before, which is what makes this safe to ship to a live fleet: 200+
node images do not have to change, and the ones where six hours is a real risk
can adopt it one at a time.

Additive + idempotent. Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260814_add_workflow_checkpoints.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


# Nullable, and NULL means "this node run has no durable state". Defaulting it
# would hand every node a volume that nothing writes to and nothing reaps —
# 3,900 orphaned volumes were cleaned off this deployment on 2026-08-13, and
# that is precisely how they accumulate.
NODE_RUN_COLUMN_SQL = """
ALTER TABLE node_runs
    ADD COLUMN IF NOT EXISTS checkpoint_volume VARCHAR(255);
"""

# `progress` is a float 0..1 and deliberately advisory: it drives a UI, never a
# decision. A node that reports 0.9 and then fails must still be retried, so
# nothing here is allowed to become a completion signal — that remains the task
# status, which is the only thing the executor actually proves.
#
# `payload` is whatever the node considered worth recording (an epoch, a frame
# index, a file list). The platform does not interpret it; the node reads its
# own back on the next attempt. Storing it means an operator can see how far a
# run had got without shelling onto the host that held it.
TABLE_SQL = """
CREATE TABLE IF NOT EXISTS workflow_checkpoints (
    id                BIGSERIAL PRIMARY KEY,
    workflow_run_id   INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    node_run_id       INTEGER NOT NULL REFERENCES node_runs(id) ON DELETE CASCADE,
    attempt           INTEGER NOT NULL DEFAULT 0,
    label             VARCHAR(255),
    progress          DOUBLE PRECISION,
    payload           JSONB,
    completed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# The composite index the change asks for (1.7). Run detail reads "the latest
# checkpoints for this run", which is this index exactly; without it that page
# degrades into a sequential scan as history accumulates.
INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_workflow_checkpoints_run_completed
    ON workflow_checkpoints (workflow_run_id, completed_at DESC);
CREATE INDEX IF NOT EXISTS ix_workflow_checkpoints_node_run
    ON workflow_checkpoints (node_run_id, completed_at DESC);
"""

STATEMENTS = [NODE_RUN_COLUMN_SQL, TABLE_SQL, INDEX_SQL]


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
    print("workflow_checkpoints ready; node_runs.checkpoint_volume added "
          "(NULL = this node run keeps no durable state).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
