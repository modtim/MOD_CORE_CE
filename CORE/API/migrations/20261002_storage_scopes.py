"""GX.S6 (launch-completion-megaplan Group X) — per-stack/app storage scopes.

Adds ONE table: the tombstone/audit + retention-hold record for objects that
live under a stack/app storage scope (a PREFIX in the tenant's file bucket —
``stacks/{stack_id}/`` or ``apps/{app_id}/`` — or the tenant's BYO-S3 bucket
when an active StorageConnection exists).

  storage_scope_objects   one row per object under a scope. SOFT-DELETE ONLY
                          (ISO 27001 — no hard deletes): a delete flips
                          status to 'deleted' + deleted_at and moves the
                          BYTES to the scope's ``.trash/`` prefix; the row is
                          never removed. ``retention_hold_until`` carries the
                          retention/compliance hold is not part of this edition (this edition gets bucket
                          + presigned links only).

No AuditAction enum values are added: the service writes ``models.AuditEvent``
rows with free-form event_name strings (the same pattern as GX.R4's
ephemeral_run_inputs), so no ``ALTER TYPE auditaction`` is required.

No reserved words appear in the DDL. Object keys are capped at 512 chars so
the UNIQUE index (prefix + key) stays under Postgres' btree key limit.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS. Safe to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261002_storage_scopes.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("storage_scope_objects",)

CREATE_STORAGE_SCOPE_OBJECTS_SQL = """
CREATE TABLE IF NOT EXISTS storage_scope_objects (
    id                   SERIAL PRIMARY KEY,
    tenant_id            INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    scope_kind           VARCHAR(16) NOT NULL,
    scope_id             BIGINT NOT NULL,
    object_key           VARCHAR(512) NOT NULL,
    status               VARCHAR(16) NOT NULL DEFAULT 'live',
    size_bytes           BIGINT,
    content_type         VARCHAR(255),
    retention_hold_until TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at           TIMESTAMPTZ,
    CONSTRAINT uq_storage_scope_objects_key
        UNIQUE (tenant_id, scope_kind, scope_id, object_key)
);
"""

INDEX_TENANT_SCOPE_SQL = """
CREATE INDEX IF NOT EXISTS ix_storage_scope_objects_tenant_scope
    ON storage_scope_objects (tenant_id, scope_kind, scope_id, status);
"""
INDEX_TENANT_KEY_SQL = """
CREATE INDEX IF NOT EXISTS ix_storage_scope_objects_tenant_key
    ON storage_scope_objects (tenant_id, scope_kind, scope_id, object_key);
"""

_STATEMENTS = [
    ("create table storage_scope_objects", CREATE_STORAGE_SCOPE_OBJECTS_SQL),
    ("index storage_scope_objects(tenant, scope, status)", INDEX_TENANT_SCOPE_SQL),
    ("index storage_scope_objects(tenant, scope, key)", INDEX_TENANT_KEY_SQL),
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
    print("storage_scope_objects ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
