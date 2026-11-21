#!/usr/bin/env python3
"""475 / expr-1.11 (workflow-expressiveness-primitives) -- add SUSPENDED and
AWAITING_EXTERNAL_EVENT to the `taskstatus` enum.

A workflow run can now sit in two new non-terminal states:
  * SUSPENDED                — the run is paused by an explicit user action
                               (pause/resume, expr-1.11); it is neither QUEUED
                               nor RUNNING but must not be treated as finished.
  * AWAITING_EXTERNAL_EVENT  — the run is blocked on an out-of-band event
                               (an external executor / human callback) and
                               resumes when the event arrives.

revision      : 20261012l_taskstatus_suspended_awaiting
down_revision : 20261012k_workflow_extends_implements

SQLAlchemy stores Python enum *member names* (UPPERCASE) as the PG enum
labels for `Column(Enum(TaskStatus))` (no values_callable), so the new
labels match the existing QUEUED/RUNNING/... convention. Verified against
the sibling test DB in tests/test_taskstatus_enum_migration.py.

CHEAP: ALTER TYPE ... ADD VALUE IF NOT EXISTS appends a label; on modern
Postgres it rewrites no rows. It is NOT idempotent pre-PG-14 for values
already in use, so IF NOT EXISTS + the current-label check below make a
re-run a clean no-op. Run OUTSIDE any transaction (ALTER TYPE takes the
autocommit raw connection, same as 20260425_add_content_rename_audit_actions).

Downgrade is a no-op (labels are additive; rollback keeps them -- nothing
is dropped, and no existing row can reference a label this migration adds
until models.py + code ship the members).

Run inside the API container:
    docker exec my_fastapi_app python3 migrations/20261012l_taskstatus_suspended_awaiting.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

ENUM_NAME = "taskstatus"
# Member NAMES of the new models.TaskStatus members (SQLAlchemy stores names,
# not values, as PG enum labels -- same convention as the 20260425 precedent).
NEW_LABELS = ["SUSPENDED", "AWAITING_EXTERNAL_EVENT"]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def _existing_values(cur) -> list[str]:
    cur.execute(
        """
        SELECT e.enumlabel
          FROM pg_type t
          JOIN pg_enum e ON t.oid = e.enumtypid
         WHERE t.typname = %s
         ORDER BY e.enumsortorder
        """,
        (ENUM_NAME,),
    )
    return [r[0] for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                    help="Apply the ALTER TYPE steps. Without this flag, "
                         "dry-run only.")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()
    current = _existing_values(cur)
    if not current:
        print(f"No existing {ENUM_NAME} enum type found "
              "(fresh/empty schema -- create_all builds it from models.py "
              "with the new members; nothing to migrate).")
        return 0

    to_add = [v for v in NEW_LABELS if v not in current]
    if not to_add:
        print(f"All labels already present on {ENUM_NAME} -- nothing to do.")
        return 0

    for v in to_add:
        stmt = f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{v}'"
        print(f"{'WRITE' if args.commit else 'DRY  '}  {stmt}")
        if args.commit:
            cur.execute(stmt)

    if args.commit:
        after = _existing_values(cur)
        print(f"Applied. {ENUM_NAME} now contains {len(after)} values "
              f"(added: {', '.join(to_add)}).")
    else:
        print("(dry run -- re-run with --commit to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
