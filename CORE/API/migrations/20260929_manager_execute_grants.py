"""GC.MWL.2 — manager-work-loans: manager_execute_grants.

Separates "may open a SESSION on this manager" from "may EXECUTE work
here". WorkLoan (migrations/20260929_work_loans.py) lets a borrowing
manager run a loaned workflow run AS the submitting user; this table is
the authorization record that decides whether that user is even allowed
to execute (or merely log in) on that specific manager, in that specific
tenant.

`manager_execute_grants`:
    id, manager_id (FK managers.manager_id), user_id (FK users.id),
    tenant_id (FK tenants.id), scope TEXT CHECK IN ('execute', 'login',
    'both'), source TEXT CHECK IN ('manual', 'idp_group') DEFAULT
    'manual', idp_group (nullable — set when source='idp_group'),
    granted_by (nullable FK users.id), created_at, deleted_at (soft
    delete — ISO 27001: NO hard deletes; revocation = soft delete).

    A partial UNIQUE index on (manager_id, user_id, tenant_id, scope)
    WHERE deleted_at IS NULL prevents duplicate LIVE grants of the same
    scope for the same (manager, user, tenant) triple while still letting
    history accumulate through soft-deleted rows.

`source` + nullable `idp_group` model BOTH possible grant origins (manual
MOD-role grant vs. SCIM-synced IdP group membership) since the product
owner hasn't decided between them yet — either answer fits without a
further schema change.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS, ALTER OWNER. Safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260929_manager_execute_grants.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys


_TABLES = ("manager_execute_grants",)

CREATE_MANAGER_EXECUTE_GRANTS_SQL = """
CREATE TABLE IF NOT EXISTS manager_execute_grants (
    id           SERIAL PRIMARY KEY,
    manager_id   VARCHAR(255) NOT NULL REFERENCES managers(manager_id),
    user_id      INTEGER NOT NULL REFERENCES users(id),
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id),
    scope        VARCHAR(20) NOT NULL,
    source       VARCHAR(20) NOT NULL DEFAULT 'manual',
    idp_group    VARCHAR(255),
    granted_by   INTEGER REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ,
    CONSTRAINT ck_manager_execute_grants_scope
        CHECK (scope IN ('execute', 'login', 'both')),
    CONSTRAINT ck_manager_execute_grants_source
        CHECK (source IN ('manual', 'idp_group'))
);
"""

MANAGER_EXECUTE_GRANTS_MANAGER_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_manager_execute_grants_manager_id
    ON manager_execute_grants (manager_id);
"""

MANAGER_EXECUTE_GRANTS_USER_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_manager_execute_grants_user_id
    ON manager_execute_grants (user_id);
"""

MANAGER_EXECUTE_GRANTS_TENANT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_manager_execute_grants_tenant_id
    ON manager_execute_grants (tenant_id);
"""

MANAGER_EXECUTE_GRANTS_DELETED_AT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_manager_execute_grants_deleted_at
    ON manager_execute_grants (deleted_at);
"""

# Partial unique index: only one LIVE grant per (manager, user, tenant,
# scope) — soft-deleted (revoked) rows are excluded so history can
# accumulate without blocking a re-grant.
MANAGER_EXECUTE_GRANTS_LIVE_UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_manager_execute_grants_live
    ON manager_execute_grants (manager_id, user_id, tenant_id, scope)
    WHERE deleted_at IS NULL;
"""

STATEMENTS = [
    ("create table manager_execute_grants", CREATE_MANAGER_EXECUTE_GRANTS_SQL),
    ("create index ix_manager_execute_grants_manager_id", MANAGER_EXECUTE_GRANTS_MANAGER_INDEX_SQL),
    ("create index ix_manager_execute_grants_user_id", MANAGER_EXECUTE_GRANTS_USER_INDEX_SQL),
    ("create index ix_manager_execute_grants_tenant_id", MANAGER_EXECUTE_GRANTS_TENANT_INDEX_SQL),
    ("create index ix_manager_execute_grants_deleted_at", MANAGER_EXECUTE_GRANTS_DELETED_AT_INDEX_SQL),
    ("create unique index ux_manager_execute_grants_live", MANAGER_EXECUTE_GRANTS_LIVE_UNIQUE_INDEX_SQL),
]


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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        for label, sql in STATEMENTS:
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        for t in _TABLES:
            try:
                cur.execute(f'ALTER TABLE {t} OWNER TO {owner}')
            except Exception as e:  # noqa: BLE001
                print(f"(ALTER TABLE {t} OWNER TO {owner} failed: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("manager_execute_grants ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
