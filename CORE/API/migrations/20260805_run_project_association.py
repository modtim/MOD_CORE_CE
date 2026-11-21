"""project-agent §3.1 — join a workflow run to the project that caused it.

``WorkflowRunEvent`` hangs off a single ``workflow_run_id``. An agent that
builds node A, builds node B, runs the pipeline, repairs B and runs it
again has produced five workflow runs with nothing recording that they
were one effort. Without that join there is no answer to "how is my
project going" short of reading five separate run pages.

This is the missing rung. The lower four already exist:

    project   <- this migration
      run     workflow_runs
      node    node_runs (inputs / outputs JSONB)
      event   workflow_run_events (source / level / message)
      stdout  container output

Schema:

  - ``workflow_runs.project_session_id`` VARCHAR(36) FK ->
    support_chat_sessions(id) ON DELETE SET NULL, indexed.

``ON DELETE SET NULL`` rather than CASCADE: deleting a project must not
delete the runs it produced. Those runs may hold the only record of work
a tenant paid for, and their audit trail outlives the conversation that
started them.

Nullable and unset for every existing run — a run with no project is the
normal case and stays so.

Run inside the API container:

    docker exec my_fastapi_app python3 \\
        migrations/20260805_run_project_association.py --commit

Idempotent — repeated runs are safe.
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


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if _column_exists(cur, "workflow_runs", "project_session_id"):
        print("Column workflow_runs.project_session_id already present.")
    else:
        stmt = (
            "ALTER TABLE workflow_runs ADD COLUMN project_session_id "
            "VARCHAR(36) REFERENCES support_chat_sessions(id) "
            "ON DELETE SET NULL"
        )
        if not args.commit:
            print(f"DRY  would: {stmt}")
        else:
            cur.execute(stmt)
            print(f"WRITE {stmt}")

    stmt = (
        "CREATE INDEX IF NOT EXISTS idx_workflow_runs_project ON "
        "workflow_runs(project_session_id) "
        "WHERE project_session_id IS NOT NULL"
    )
    if not args.commit:
        print(f"DRY  would: {stmt}")
    else:
        cur.execute(stmt)
        print(f"WRITE {stmt}")

    print("Done." if args.commit else "Dry run complete. Re-run with --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
