"""project_handoffs table — project-agent groups 6-10 (mega-completion-plan WS-3).

The durable handoff record: a parked task's question to a human (kind=question)
or the node-wizard payload dispatched to a child session (kind=wizard_dispatch),
closing the gap where project_loop.tick built a payload nothing consumed. One
table, tenant-scoped, indexed on (project_session_id, status).

Base.metadata.create_all() at startup also creates this if missing; this migration
creates it explicitly for deploy + re-owns it to the portable owner role.

CE-MIGROLE-3c4: the re-own is AUTOMATIC — the migration resolves the owner at
runtime through `_owner_role.owner_role(cur)` (the SaaS superuser role when it
exists, otherwise the connection's current user on a CE install), so there is no
manual `psql ... ALTER TABLE ... OWNER TO` follow-up step for an operator to run.
Re-owning is idempotent: it runs whenever the table exists, whether this run
created it or a previous run/`create_all()` did.

Run inside the API container:
    docker exec my_fastapi_app python3 migrations/20260902_project_handoffs.py --commit
Rollback: DROP TABLE project_handoffs;
"""
from __future__ import annotations

import argparse
import os
import sys


def _reown(eng, table: str) -> None:
    """ALTER TABLE <table> OWNER TO <resolved owner> — idempotent, guarded.

    SQLAlchemy's `__table__.create` runs as whatever transient role connected
    (Vault rotates the API's Postgres role between connections), so a table it
    just made would otherwise be owned by a role that stops existing on the next
    rotation.  Re-owning through the same engine's DBAPI cursor mirrors the
    psycopg2 migrations: `_owner_role.owner_role(cur)` returns the QUOTED token
    for whoever owns DB objects on THIS database.  The ALTER is skipped when the
    table is absent, and a failed ALTER is reported, never fatal.
    """
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    cur = dbapi.cursor()
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import _owner_role  # CE-MIGROLE-3c4: resolve the owner role at runtime

        owner = _owner_role.owner_role(cur)
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            f"WHERE table_name = '{table}'"
        )
        if cur.fetchone() is None:
            print(f"({table}: table absent — skipping OWNER TO)")
            return
        try:
            cur.execute(f"ALTER TABLE {table} OWNER TO {owner}")
        except Exception as e:  # noqa: BLE001
            print(f"(ALTER TABLE {table} OWNER TO {owner} failed: {e})")
            return
        print(f"WRITE ALTER TABLE {table} OWNER TO {owner}")
    finally:
        cur.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
    import models  # noqa: F401
    from models import ProjectHandoff

    with eng.connect() as c:
        exists = c.exec_driver_sql(
            "SELECT 1 FROM information_schema.tables WHERE table_name='project_handoffs'"
        ).fetchone() is not None
    if exists:
        print("Table project_handoffs already present.")
        _reown(eng, "project_handoffs")
        return 0
    if not args.commit:
        print("DRY  would: CREATE TABLE project_handoffs (from ProjectHandoff model)")
        print("DRY  would: ALTER TABLE project_handoffs OWNER TO <resolved owner>")
        return 0

    ProjectHandoff.__table__.create(bind=eng, checkfirst=True)
    print("WRITE CREATE TABLE project_handoffs")
    _reown(eng, "project_handoffs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
