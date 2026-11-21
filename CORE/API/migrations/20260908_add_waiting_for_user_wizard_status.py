"""conversational-project-builder §3.3 — add ``WAITING_FOR_USER`` to the
Wizard Queue status enum.

A conversational project that hands a checkpoint to the user pauses its
Wizard Queue run in a NEW, non-terminal ``waiting-for-user`` state (durable,
resumable, never auto-failed). The canonical lifecycle enum
``wizard_run_status_enum`` needs the label before any row can be written with
it.

SQLAlchemy stores the UPPERCASE Python member NAME as the enum label (see
20260907_add_wizard_runs.py), so the label added here is ``WAITING_FOR_USER``
(member name), not the kebab wire value ``waiting-for-user``.

``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` is idempotent and must run OUTSIDE a
transaction block, so this uses the autocommit admin connection (same one the
sibling migrations use for triggers/ownership). A fresh ``create_all`` DB (e.g.
the test schema) already gets the label from the Python enum — this migration
is only for existing prod databases whose enum type predates the label.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260908_add_waiting_for_user_wizard_status.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


_ENUM_TYPE = "wizard_run_status_enum"
_NEW_LABEL = "WAITING_FOR_USER"


def _get_admin_conn():
    """Direct modTech connection with autocommit — ALTER TYPE ADD VALUE cannot
    run inside a transaction block. Same approach as the sibling migrations."""
    import psycopg2  # type: ignore

    user = os.environ.get("POSTGRES_USER")
    password = os.environ.get("POSTGRES_PASSWORD")
    db = os.environ.get("POSTGRES_DB")
    host = os.environ.get("POSTGRES_HOST", "postgres-app")
    port = os.environ.get("POSTGRES_PORT", "5432")

    if not (user and password and db):
        raise RuntimeError(
            "POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB env vars missing "
            "— run this migration inside my_fastapi_app where they're "
            "populated by docker-compose."
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

    stmt = (
        f"ALTER TYPE {_ENUM_TYPE} "
        f"ADD VALUE IF NOT EXISTS '{_NEW_LABEL}' AFTER 'WAITING_FOR_WORKER'"
    )
    print(f"{'WRITE' if args.commit else 'DRY  '}  add enum label "
          f"{_ENUM_TYPE}.{_NEW_LABEL}")
    print(f"        {stmt}")

    if args.commit:
        conn = _get_admin_conn()
        cur = conn.cursor()
        cur.execute(stmt)
        print("Applied.")
    else:
        print("(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
