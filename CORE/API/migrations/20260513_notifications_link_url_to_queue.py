"""Notifications — migrate node-generation link_url from wizard-resume to queue-panel.

Companion to commit 89a00cb0. The notification creator
(`worker_tasks.py:_emit_generation_notification`) used to set
``link_url`` to ``#workflow-builder?resume=<task_id>`` for both
``node_generation_complete`` and ``node_generation_failed`` rows.
The Dashboard's notifications bell follows the link verbatim, so
clicks dropped the user into the wizard's Review step instead of
the build-queue panel where they expected to see their task.

After 89a00cb0 new notifications use:

    #workflow-builder?openQueue=1&taskId=<task_id>

…which the WorkflowBuilder's mount-effect translates into "open
the queue panel + expand the row for this task." This migration
rewrites every existing notification with the old shape to match,
so already-delivered alerts in tenants' inboxes also route to the
queue when clicked.

Idempotent: only touches rows whose ``link_url`` starts with the
exact legacy prefix, so re-running after a successful commit is a
no-op.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260513_notifications_link_url_to_queue.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


_LEGACY_PREFIX = "#workflow-builder?resume="
_TARGET_TYPES = ("node_generation_complete", "node_generation_failed")


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

    # Preview / sanity check.
    cur.execute(
        """
        SELECT notification_type, COUNT(*)
          FROM notifications
         WHERE link_url LIKE %s
           AND notification_type = ANY(%s)
         GROUP BY notification_type
         ORDER BY 1
        """,
        (_LEGACY_PREFIX + "%", list(_TARGET_TYPES)),
    )
    rows = cur.fetchall()
    if not rows:
        print("Schema already current — no notifications on the legacy link_url shape.")
        return 0

    total = 0
    for nt, count in rows:
        print(f"  {'WRITE' if args.commit else 'DRY  '}  would migrate {count:>4d} row(s) of notification_type={nt}")
        total += count
    print(f"\nTotal: {total} row(s)")

    if not args.commit:
        print("\n(dry run — re-run with --commit)")
        return 0

    # REPLACE on the prefix is safe because the legacy shape is
    # always exactly `?resume=<id>` — no other notification source
    # emits this prefix (audited 2026-05-13).
    cur.execute(
        """
        UPDATE notifications
           SET link_url = REPLACE(link_url, '?resume=', '?openQueue=1&taskId=')
         WHERE link_url LIKE %s
           AND notification_type = ANY(%s)
        """,
        (_LEGACY_PREFIX + "%", list(_TARGET_TYPES)),
    )
    updated = cur.rowcount

    # Verify zero rows remain on the old shape (idempotency check).
    cur.execute(
        "SELECT COUNT(*) FROM notifications WHERE link_url LIKE %s",
        (_LEGACY_PREFIX + "%",),
    )
    leftover = cur.fetchone()[0]
    if leftover:
        print(
            f"\nWARNING: {leftover} row(s) still on the legacy shape after migration. "
            "They have a notification_type outside the target set — left alone "
            "intentionally."
        )

    print(f"\nApplied. {updated} row(s) updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
