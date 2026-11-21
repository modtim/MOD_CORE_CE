"""ApiTaskQueue — add error_detail (calibration-health-fixtures-and-timeouts §3).

Today's failed calibration tasks store only `error_message`, which for the
most common failure class truncates to something like:

    "Added myminio successfully.   \\nmc"

The `mc` sidecar's stdout success banner ("Added myminio successfully.")
lands in FRONT of its stderr (the actual reason: missing key / missing
bucket / auth failure), and whatever assembled the stored string cut it off
right after the banner — the diagnostic end is exactly what's missing. An
operator triaging a FAILED calibration row could not tell a missing key from
a missing bucket from an auth failure without re-running the task.

design.md's Decision ("Diagnostic surface: classify, don't just truncate")
chose option (a): capture the failing tool's raw stderr in a new column,
kept separate from `error_message` rather than appended to it, so a longer
banner can never crowd the diagnostic part out again. Structured
classification (key-not-found / bucket-not-found / access-denied) is an
explicit follow-up, not part of this change.

Companion code:
  - models.py: `ApiTaskQueue.error_detail` (Text, nullable) ORM column.
  - routers/worker_tasks.py: `TaskFailedRequest.error_detail`, written to
    `task.error_detail` on both the requeue and terminal-failure branches of
    POST /workers/tasks/{id}/failed.
  - Worker/internal/executor/executor.go: `SidecarExitError` keeps a failing
    sidecar's stdout/stderr separate (instead of concatenating them) and
    `sidecarErrorResult` surfaces the stderr half as `Result.ErrorDetail`,
    which cmd/worker/main.go forwards through the outbox/transport chain as
    the new `error_detail` field on the failure report.

This migration:
  1. Adds `api_task_queue.error_detail` (TEXT, nullable). Idempotent ALTER —
     safe to run against a database that already has the column (e.g. after
     a partial apply) and safe to run twice.
  2. No backfill — existing FAILED rows keep `error_detail IS NULL`; only
     failures reported by a worker built after this change populate it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260901_api_task_queue_error_detail.py --commit

Rollback: `ALTER TABLE api_task_queue DROP COLUMN error_detail;` (also drop
the companion code changes — single-commit revert per design.md §9).
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'api_task_queue'
           AND column_name = 'error_detail'
        """
    )
    col_exists = cur.fetchone() is not None
    if col_exists:
        print("Column api_task_queue.error_detail already present.")
    else:
        if not args.commit:
            print("DRY  would: ALTER TABLE api_task_queue ADD COLUMN "
                  "error_detail TEXT")
        else:
            cur.execute("ALTER TABLE api_task_queue ADD COLUMN error_detail TEXT")
            print("WRITE ALTER TABLE api_task_queue ADD COLUMN error_detail TEXT")

    if not args.commit:
        print("\n(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
