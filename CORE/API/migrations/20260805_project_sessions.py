"""project-agent §2.1/§2.2 — durable project context on the chat session.

A project session is a ``support_chat_sessions`` row with
``kind='project'``. Its children (``parent_session_id``, which already
exists) are the wizard conversations the agent dispatches — that is how
the agent holds context the wizards do not, so the node wizard keeps
receiving only expected input, the process needed, and expected output.

The columns below hold the part of a project that design D2 says is
NEVER summarised. They live on the row rather than in the transcript
because a project running for weeks will exceed any context window, and
what must survive that is the plan, the criteria, the example, the
artifact inventory, and the record of what has already been tried and
failed. Summarising a failure history makes the agent retry what it
already knows does not work — the exact failure never-stop amplifies.

Columns (all on ``support_chat_sessions``, all NULL/empty for other kinds):

  - ``project_need`` TEXT
  - ``project_criteria`` JSONB — the opening ask: a representative input
    and the required output. Also becomes ``BuildSpec.sample_output`` for
    every node built for the project (§1.1).
  - ``project_plan`` JSONB
  - ``project_plan_history`` JSONB NOT NULL DEFAULT '[]' — superseded
    revisions, append-only
  - ``project_artifacts`` JSONB NOT NULL DEFAULT '[]'
  - ``project_failure_history`` JSONB NOT NULL DEFAULT '{}'
  - ``project_status`` VARCHAR(24) — active | parked | complete
  - ``project_completed_at`` TIMESTAMPTZ

No backfill: no project sessions exist yet. Existing sessions of every
other kind are untouched and keep working.

Run inside the API container:

    docker exec my_fastapi_app python3 \\
        migrations/20260805_project_sessions.py --commit

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


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return cur.fetchone() is not None


_COLUMNS = [
    ("project_need", "TEXT"),
    ("project_criteria", "JSONB"),
    ("project_plan", "JSONB"),
    ("project_plan_history", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
    ("project_artifacts", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
    ("project_failure_history", "JSONB NOT NULL DEFAULT '{}'::jsonb"),
    ("project_status", "VARCHAR(24)"),
    ("project_completed_at", "TIMESTAMPTZ"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if not _table_exists(cur, "support_chat_sessions"):
        print("Table support_chat_sessions absent — nothing to do.")
        return 1

    for name, ddl in _COLUMNS:
        if _column_exists(cur, "support_chat_sessions", name):
            print(f"Column support_chat_sessions.{name} already present.")
            continue
        stmt = f"ALTER TABLE support_chat_sessions ADD COLUMN {name} {ddl}"
        if not args.commit:
            print(f"DRY  would: {stmt}")
        else:
            cur.execute(stmt)
            print(f"WRITE {stmt}")

    # A project's children are looked up by parent on every resume, and
    # active projects are swept for parked work.
    idx = [
        ("idx_scs_parent", "support_chat_sessions(parent_session_id) "
                           "WHERE parent_session_id IS NOT NULL"),
        ("idx_scs_project", "support_chat_sessions(tenant_id, project_status) "
                            "WHERE kind = 'project'"),
    ]
    for name, spec in idx:
        stmt = f"CREATE INDEX IF NOT EXISTS {name} ON {spec}"
        if not args.commit:
            print(f"DRY  would: {stmt}")
        else:
            cur.execute(stmt)
            print(f"WRITE {stmt}")

    print("Done." if args.commit else "Dry run complete. Re-run with --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
