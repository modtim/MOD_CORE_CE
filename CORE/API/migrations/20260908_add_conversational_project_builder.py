"""conversational-project-builder §1.1–1.4 — man-in-the-middle Project builder.

FOUNDATION phase (data model + flag only; no behaviour). Layers the
conversational collaboration loop onto the existing project-agent (a
``SupportChatSession`` with ``kind="project"``) — it EXTENDS that project, it
does not create a parallel Project concept.

Adds:

  * column ``support_chat_sessions.project_gate_state`` — the project-level
    collaboration gate (aligning | building | awaiting-user | done). NULL on
    every legacy one-shot project; a distinct axis from ``project_status``.
  * ``project_conversation_turns``  (§1.1) — align-before-build conversation,
    author/role/body/intent?, append-only + soft-delete.
  * ``project_checklist_tasks``     (§1.2) — the shared opsx-style ordered
    checklist (todo|building|awaiting-test|accepted|reworking), soft-delete.
  * ``project_checkpoints``         (§1.3) — the structured hand-off linking a
    checklist task to {artifact_ref, artifact_link, what_to_check}.

The table DDL is materialised through ``Base.metadata.create_all`` so it is
byte-for-byte identical to what the app's startup bootstrap produces
(``database.create_tables``) — create_all is idempotent, so running this after
a restart that already created the tables is a no-op. Same approach as
migrations/20260907_add_wizard_runs.py.

The one thing create_all does NOT do, and part of why this migration exists,
is the ISO 27001 no-hard-delete enforcement: a BEFORE DELETE (row) + BEFORE
TRUNCATE (statement) trigger on each of the three new tables that raises,
allowing UPDATE (status transitions, intent override, setting ``deleted_at``)
but forbidding physical removal. Removal is always a soft-delete via
``deleted_at``.

Idempotent: the ADD COLUMN uses IF NOT EXISTS; create_all skips existing
tables; the trigger function uses CREATE OR REPLACE and the triggers use
DROP IF EXISTS + CREATE.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260908_add_conversational_project_builder.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


_NEW_TABLES = (
    "project_conversation_turns",
    "project_checklist_tasks",
    "project_checkpoints",
)


def _get_engine():
    """The app's Vault-rotated engine — used for create_all so the schema
    matches the startup bootstrap exactly."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore

    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
    return eng


def _get_admin_conn():
    """Direct POSTGRES_USER connection, bypassing Vault rotation. ALTER OWNER and
    CREATE TRIGGER need a stable owning role — Vault roles rotate every few
    minutes and lose object ownership otherwise. Same approach as
    migrations/20260907_add_wizard_runs.py."""
    import psycopg2  # type: ignore

    user = os.environ.get("POSTGRES_USER")
    password = os.environ.get("POSTGRES_PASSWORD")
    db = os.environ.get("POSTGRES_DB")
    host = os.environ.get("POSTGRES_HOST", "postgres-app")
    port = os.environ.get("POSTGRES_PORT", "5432")

    if not (user and password and db):
        raise RuntimeError(
            "POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB env vars "
            "missing — run this migration inside my_fastapi_app where "
            "they're populated by docker-compose."
        )

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from database import _pg_ssl_connect_args  # GSEC R-057 (inert)
    conn = psycopg2.connect(
        user=user, password=password, dbname=db, host=host, port=port,
        **_pg_ssl_connect_args(),
    )
    conn.autocommit = True
    return conn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import models  # type: ignore

    tables = [
        models.ProjectConversationTurn.__table__,
        models.ProjectChecklistTask.__table__,
        models.ProjectCheckpoint.__table__,
    ]

    # --- Step 1: create the three tables (idempotent) -----------------
    print(f"{'WRITE' if args.commit else 'DRY  '}  create tables via "
          f"Base.metadata.create_all: {[t.name for t in tables]}")
    if args.commit:
        engine = _get_engine()
        models.Base.metadata.create_all(bind=engine, tables=tables)

    # --- Step 2: gate column, ownership, no-hard-delete triggers -------
    add_column_stmt = (
        'add column support_chat_sessions.project_gate_state',
        "ALTER TABLE support_chat_sessions "
        "ADD COLUMN IF NOT EXISTS project_gate_state VARCHAR(24)",
    )

    trigger_fn = """
        CREATE OR REPLACE FUNCTION project_builder_no_hard_delete() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'Hard delete/truncate is forbidden on % (ISO no-hard-delete); '
                'use soft-delete (set deleted_at) instead',
                TG_TABLE_NAME
                USING ERRCODE = 'feature_not_supported';
        END;
        $$ LANGUAGE plpgsql
    """

    conn = _get_admin_conn()
    cur = conn.cursor()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — re-own to the current user
    owner = _owner_role.owner_role(cur)

    owner_stmts = [
        (f'ALTER OWNER {t} -> {owner}',
         f'ALTER TABLE {t} OWNER TO {owner}')
        for t in _NEW_TABLES
    ]

    trigger_stmts = [
        ('create or replace project_builder_no_hard_delete() function',
         trigger_fn),
    ]
    for t in _NEW_TABLES:
        trigger_stmts.extend([
            (f'drop trigger trg_no_delete_{t} if exists',
             f'DROP TRIGGER IF EXISTS trg_no_delete_{t} ON {t}'),
            (f'create trigger trg_no_delete_{t} BEFORE DELETE (row)',
             f"""
             CREATE TRIGGER trg_no_delete_{t}
                 BEFORE DELETE ON {t}
                 FOR EACH ROW EXECUTE FUNCTION project_builder_no_hard_delete()
             """),
            (f'drop trigger trg_no_truncate_{t} if exists',
             f'DROP TRIGGER IF EXISTS trg_no_truncate_{t} ON {t}'),
            (f'create trigger trg_no_truncate_{t} BEFORE TRUNCATE (stmt)',
             f"""
             CREATE TRIGGER trg_no_truncate_{t}
                 BEFORE TRUNCATE ON {t}
                 FOR EACH STATEMENT EXECUTE FUNCTION project_builder_no_hard_delete()
             """),
        ])

    all_stmts = [add_column_stmt] + owner_stmts + trigger_stmts
    for label, stmt in all_stmts:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        print(f"        {stmt.strip()[:200]}")

    if args.commit:
        for _label, stmt in all_stmts:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
