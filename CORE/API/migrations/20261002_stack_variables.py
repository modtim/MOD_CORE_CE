"""GX.S10a (launch-completion-megaplan Group X) — stack variables + secrets.

Creates the ``stack_secret_index`` table (the DB mirror of the tenant's
Vault-backed secrets: Vault LIST is not granted to the API approle, so the
"existing secrets" dropdown is served from here — append/soft-delete only,
ISO 27001, no hard deletes) and adds ``persistent_stacks.needs_redeploy``
(rotation of a stack variable is a pending change the operator must redeploy
to apply).

``stack_secret_index.path`` is a column named after a SQL reserved word
(PATH), so it is quoted everywhere.

Additive only; idempotent: CREATE TABLE/INDEX IF NOT EXISTS,
ADD COLUMN IF NOT EXISTS. Safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261002_stack_variables.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("stack_secret_index",)

CREATE_STACK_SECRET_INDEX_SQL = """
CREATE TABLE IF NOT EXISTS stack_secret_index (
    id                     SERIAL PRIMARY KEY,
    tenant_id              INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- full Vault KV path (the `vault:` ref shape), e.g.
    -- tenants/7/stacks/strata/vars/STRATA_API_KEY — PATH is a reserved
    -- word, so it is quoted.
    "path"                 VARCHAR(512) NOT NULL,
    stack_name             VARCHAR(255) NOT NULL,
    -- the variable/secret NAME (last path segment) for display.
    name                   VARCHAR(255) NOT NULL,
    updated_by_user_id     INTEGER REFERENCES users(id),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- ISO 27001: soft-delete only; a rotation supersede stamps deleted_at
    -- on the prior live row and inserts a fresh one.
    deleted_at             TIMESTAMPTZ
);
"""

INDEX_STACK_SECRET_INDEX_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_stack_secret_index_tenant_id
    ON stack_secret_index (tenant_id);
"""
# The catalog query: a tenant's live rows, newest first.
INDEX_STACK_SECRET_INDEX_TENANT_LIVE_SQL = """
CREATE INDEX IF NOT EXISTS ix_stack_secret_index_tenant_live
    ON stack_secret_index (tenant_id, updated_at DESC)
    WHERE deleted_at IS NULL;
"""
# The upsert lookup: one live row per path.
UQ_STACK_SECRET_INDEX_LIVE_PATH_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_stack_secret_index_live_path
    ON stack_secret_index (tenant_id, "path")
    WHERE deleted_at IS NULL;
"""

ADD_NEEDS_REDEPLOY_SQL = """
ALTER TABLE persistent_stacks
    ADD COLUMN IF NOT EXISTS needs_redeploy BOOLEAN NOT NULL DEFAULT false;
"""

_STATEMENTS = [
    ("create table stack_secret_index", CREATE_STACK_SECRET_INDEX_SQL),
    ("index stack_secret_index.tenant_id", INDEX_STACK_SECRET_INDEX_TENANT_SQL),
    ("index stack_secret_index(tenant_id, updated_at) live",
     INDEX_STACK_SECRET_INDEX_TENANT_LIVE_SQL),
    ("unique stack_secret_index(tenant_id, path) WHERE deleted_at IS NULL",
     UQ_STACK_SECRET_INDEX_LIVE_PATH_SQL),
    ("persistent_stacks += needs_redeploy", ADD_NEEDS_REDEPLOY_SQL),
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
        for label, sql in _STATEMENTS:
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
    for label, sql in _STATEMENTS:
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
    print("stack variables + secrets ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
