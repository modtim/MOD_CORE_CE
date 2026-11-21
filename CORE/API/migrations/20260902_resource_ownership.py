"""resource_ownership table — authz-derived-ownership DO-4 (team/shared-drive
ownership).

Adds the authoritative owner record for a resource, which may be a USER or a
TEAM. One row per resource (unique on resource_type+resource_id). When present it
wins over the legacy owner pointers (cog.owner_id / workflow.created_by_user_id);
when absent, the derived-owner rule falls back to those pointers, so existing
resources are unaffected until explicitly (re)assigned.

Companion code:
  - models.py: PrincipalType enum + ResourceOwnership model.
  - resource_permissions.py: _derived_owner_allows consults this table first
    (user or team membership), set_resource_owner upserts it, transfer_ownership
    delegates to it.
  - routers/capabilities.py: transfer-ownership endpoint accepts new_owner_team_id.

`Base.metadata.create_all()` at startup also creates this table if missing; this
migration exists so a deploy creates it explicitly AND re-owns it to the portable
owner role (the app connects as a rotating Vault dynamic role, so a table it
creates would otherwise be owned by a transient role).

CE-MIGROLE-3c4: the re-own is AUTOMATIC — the migration resolves the owner at
runtime through `_owner_role.owner_role(cur)` (the SaaS superuser role when it
exists, otherwise the connection's current user on a CE install), so there is no
manual `psql ... ALTER TABLE ... OWNER TO` follow-up step for an operator to run.
Re-owning is idempotent: it runs whenever the table exists, whether this run
created it or a previous run/`create_all()` did.

Run inside the API container:
    docker exec my_fastapi_app python3 migrations/20260902_resource_ownership.py --commit

Rollback: DROP TABLE resource_ownership; (+ revert the companion code).
"""
from __future__ import annotations

import argparse
import os
import sys

# Every table this migration creates.  Keep in sync with the models it binds:
# ResourceOwnership contributes exactly one table (its UniqueConstraint and
# Index in __table_args__ are not separately-owned objects).
TABLES = ("resource_ownership",)


def _reown(eng, tables) -> None:
    """ALTER TABLE <t> OWNER TO <resolved owner> for each table — idempotent, guarded.

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
        for table in tables:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                f"WHERE table_name = '{table}'"
            )
            if cur.fetchone() is None:
                print(f"({table}: table absent — skipping OWNER TO)")
                continue
            try:
                cur.execute(f"ALTER TABLE {table} OWNER TO {owner}")
            except Exception as e:  # noqa: BLE001
                print(f"(ALTER TABLE {table} OWNER TO {owner} failed: {e})")
                continue
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

    import models  # noqa: F401  (registers the model on Base.metadata)
    from models import ResourceOwnership

    insp_sql = (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'resource_ownership'"
    )
    with eng.connect() as c:
        exists = c.exec_driver_sql(insp_sql).fetchone() is not None

    if exists:
        print("Table resource_ownership already present.")
        _reown(eng, TABLES)
        return 0
    if not args.commit:
        print("DRY  would: CREATE TABLE resource_ownership (from ResourceOwnership model)")
        for _t in TABLES:
            print(f"DRY  would: ALTER TABLE {_t} OWNER TO <resolved owner>")
        print("\n(dry run — re-run with --commit)")
        return 0

    ResourceOwnership.__table__.create(bind=eng, checkfirst=True)
    print("WRITE CREATE TABLE resource_ownership")
    _reown(eng, TABLES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
