"""wizard-queue-redesign §1.1 — WizardRun + WizardRunLog tables.

Creates the two tables that back the unified Wizard Queue (design D7):

  * ``wizard_runs``     — one row per long-running wizard run (the single
                          source of truth for the live console + persisted
                          history). Soft-delete only (ISO: NO hard delete).
  * ``wizard_run_logs`` — persisted, already-redacted log lines under the
                          system-log ROTATING retention policy (D20); NO
                          delete-blocking trigger so rotation can trim it.

The table/enum DDL is materialised through ``Base.metadata.create_all``
so it is byte-for-byte identical to what the app's startup bootstrap
(``database.create_tables``) produces — this avoids any enum-label drift
between a hand-written ``CREATE TYPE`` and SQLAlchemy's default (which
stores the UPPERCASE Python member names). create_all is idempotent, so
running this after a restart that already created the tables is a no-op.

The one thing create_all does NOT do, and the reason this migration
exists, is the ISO no-hard-delete enforcement: a BEFORE DELETE (row) and
BEFORE TRUNCATE (statement) trigger on ``wizard_runs`` that raises,
allowing UPDATE (status transitions + soft-delete via ``deleted_at``)
but forbidding physical removal. ``wizard_run_logs`` deliberately gets
NO such trigger — the retention job must be able to rotate old rows out.

Idempotent: create_all skips existing tables; the trigger function uses
CREATE OR REPLACE and the triggers use DROP IF EXISTS + CREATE.

No backfill (D16): only new runs are written; legacy node_build_jobs /
history are NOT migrated.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260907_add_wizard_runs.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_engine():
    """The app's Vault-rotated engine — used for create_all so the
    schema matches the startup bootstrap exactly."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore

    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
    return eng


def _get_admin_conn():
    """Direct POSTGRES_USER connection, bypassing Vault rotation. Creating
    triggers / ALTER OWNER needs a stable owning role — Vault roles
    rotate every few minutes and lose object ownership otherwise. Same
    approach as migrations/20260508_wizard_event_tables_append_only.py."""
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

    tables = [models.WizardRun.__table__, models.WizardRunLog.__table__]

    # --- Step 1: create the tables + enum types (idempotent) ----------
    print(f"{'WRITE' if args.commit else 'DRY  '}  create tables + enums via "
          f"Base.metadata.create_all: {[t.name for t in tables]}")
    if args.commit:
        engine = _get_engine()
        models.Base.metadata.create_all(bind=engine, tables=tables)

    # --- Step 2: fix ownership + install no-hard-delete trigger --------
    trigger_fn = """
        CREATE OR REPLACE FUNCTION wizard_run_no_hard_delete() RETURNS trigger AS $$
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
        (f'ALTER OWNER wizard_runs -> {owner}',
         f'ALTER TABLE wizard_runs OWNER TO {owner}'),
        (f'ALTER OWNER wizard_run_logs -> {owner}',
         f'ALTER TABLE wizard_run_logs OWNER TO {owner}'),
    ]

    trigger_stmts = [
        ('create or replace wizard_run_no_hard_delete() function', trigger_fn),
        ('drop trigger trg_no_delete_wizard_runs if exists',
         'DROP TRIGGER IF EXISTS trg_no_delete_wizard_runs ON wizard_runs'),
        ('create trigger trg_no_delete_wizard_runs BEFORE DELETE (row)',
         """
         CREATE TRIGGER trg_no_delete_wizard_runs
             BEFORE DELETE ON wizard_runs
             FOR EACH ROW EXECUTE FUNCTION wizard_run_no_hard_delete()
         """),
        ('drop trigger trg_no_truncate_wizard_runs if exists',
         'DROP TRIGGER IF EXISTS trg_no_truncate_wizard_runs ON wizard_runs'),
        ('create trigger trg_no_truncate_wizard_runs BEFORE TRUNCATE (stmt)',
         """
         CREATE TRIGGER trg_no_truncate_wizard_runs
             BEFORE TRUNCATE ON wizard_runs
             FOR EACH STATEMENT EXECUTE FUNCTION wizard_run_no_hard_delete()
         """),
    ]

    for label, stmt in owner_stmts + trigger_stmts:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        print(f"        {stmt.strip()[:200]}")

    if args.commit:
        for _label, stmt in owner_stmts + trigger_stmts:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
