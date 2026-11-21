"""wizard-learning-from-feedback §1.3 — append-only enforcement.

Adds STATEMENT-level BEFORE triggers on wizard_feedback_event and
wizard_finding_action_event that raise on UPDATE / DELETE / TRUNCATE.

Why STATEMENT-level (not ROW-level): pg13 forbids BEFORE ROW
triggers on a PARTITIONED parent — they can only live on each
child partition. STATEMENT-level triggers fire for any operation
on the table they're attached to. We attach to the parent AND each
child so direct child-partition mutations are also caught.

Why this script connects as the admin user directly: §1.1 and §1.2 ran
through `database.py`'s Vault-rotated dynamic connection, which
created the wizard tables owned by short-lived Vault roles. Trigger
creation requires OWNER privilege, and a fresh Vault role is no
longer the owner after rotation. This script:
  1. Resolves the owner at runtime via `_owner_role.owner_role(cur)`:
     modTech when that role exists (SaaS), else the current DB user
     (a CE fresh install has no modTech role).
  2. ALTER TABLE ... OWNER TO <that role> for the wizard tables — fixes
     the ownership orphan so future admin migrations work.
  3. Creates the trigger function + statement triggers on parent
     and every child partition.

Idempotent — uses CREATE OR REPLACE for the function and DROP +
CREATE for the triggers (Postgres has no `CREATE TRIGGER IF NOT
EXISTS` until pg14).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260508_wizard_event_tables_append_only.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_admin_conn():
    """Direct modTech connection, bypassing Vault rotation. DDL
    operations (CREATE TRIGGER, ALTER OWNER) need a stable
    super-role — Vault roles rotate every few minutes and lose
    object ownership across migrations otherwise."""
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


def _list_partitions(cur, parent: str) -> list[str]:
    cur.execute(
        """
        SELECT inhrelid::regclass::text
        FROM pg_inherits
        WHERE inhparent = %s::regclass
        """,
        (parent,),
    )
    return [row[0] for row in cur.fetchall()]


_WIZARD_TABLES = (
    "wizard_feedback_event",
    "wizard_finding_action_event",
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_admin_conn()
    cur = conn.cursor()

    steps: list[tuple[str, str]] = []

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — re-own to the current user
    owner = _owner_role.owner_role(cur)

    targets: list[str] = []
    for parent in _WIZARD_TABLES:
        targets.append(parent)
        targets.extend(_list_partitions(cur, parent))

    for tbl in targets:
        steps.append((
            f"ALTER OWNER {tbl} -> {owner}",
            f"ALTER TABLE {tbl} OWNER TO {owner}",
        ))

    steps.append((
        "create or replace wizard_event_no_mutate trigger function",
        """
        CREATE OR REPLACE FUNCTION wizard_event_no_mutate() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Operation % is forbidden on append-only event table %',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'feature_not_supported';
        END;
        $$ LANGUAGE plpgsql
        """,
    ))

    for tbl in targets:
        for op in ("UPDATE", "DELETE", "TRUNCATE"):
            short = tbl.split(".")[-1]
            trigger_name = f"trg_no_{op.lower()}_{short}"[:63]
            steps.append((
                f"drop trigger {trigger_name} if exists on {tbl}",
                f"DROP TRIGGER IF EXISTS {trigger_name} ON {tbl}",
            ))
            steps.append((
                f"create trigger {trigger_name} BEFORE {op} on {tbl}",
                f"""
                CREATE TRIGGER {trigger_name}
                    BEFORE {op} ON {tbl}
                    FOR EACH STATEMENT EXECUTE FUNCTION wizard_event_no_mutate()
                """,
            ))

    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        print(f"        {stmt.strip()[:200]}")
        if args.commit:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
