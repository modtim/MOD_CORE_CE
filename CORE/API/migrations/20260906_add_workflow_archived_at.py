"""workflow archive (soft-hide, distinct from soft-delete)

Adds `workflows.archived_at`. Archiving a workflow sets this timestamp so the
workflow is hidden from the default Load-workflow picker WITHOUT deleting it
(deleted_at stays reserved for the "delete entire workflow" action). Archive is
reversible — unarchive clears the column. Additive, inert on existing rows
(archived_at stays NULL → workflow visible, exactly as before). Idempotent.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260906_add_workflow_archived_at.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ADD_WORKFLOW_ARCHIVED_AT_SQL = """
ALTER TABLE workflows
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_workflows_archived_at ON workflows (archived_at);
"""

STATEMENTS = [ADD_WORKFLOW_ARCHIVED_AT_SQL]


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
    print("workflows.archived_at ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
