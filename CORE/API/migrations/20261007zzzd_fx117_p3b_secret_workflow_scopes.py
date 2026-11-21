#!/usr/bin/env python3
"""GSEC FX.117 p3b — per-secret allowed-workflows scope (models.SecretWorkflowScope).

FX.117 (owner Q104=A) ships the customer secret store. p3a added exact-value
masking of the tenant secret VALUES a run injects. p3b lets a tenant admin
restrict a tenant secret so ONLY the listed workflows may resolve it: a secret
with NO active scope rows behaves exactly as today (every workflow of the
tenant).

NEW TABLE ONLY (models.py is bind-mounted live: no column was added to any
existing table). The p2 `Secret.allowed_workflow_ids` JSONB bag stays as the
admin-facing view; this table is its authoritative backing store, so a scope
becomes a RECORD the compliance trail can read (which workflow a secret was
allowed for, when the allowance was added and when it was removed).

SOFT-DELETE ONLY (ISO 27001 — replacing a scope soft-deletes the removed rows
and stamps `deleted_at`; nothing is ever hard-deleted), and the secret_id FK
carries NO `ON DELETE CASCADE` clause: deleting a secret must not drop the
record of what scope it once owned.

    secret_workflow_scopes
      id                  BIGSERIAL PRIMARY KEY
      tenant_id           INTEGER NOT NULL  FK tenants(id)   owner tenant
      secret_id           INTEGER NOT NULL  FK secrets(id)   (no cascade)
      workflow_id         INTEGER NOT NULL  FK workflows(id) (no cascade)
      created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
      created_by_user_id  INTEGER NULL      FK users(id)
      deleted_at          TIMESTAMPTZ NULL  soft delete ONLY
      deleted_by_user_id  INTEGER NULL      FK users(id)

Constraints (the DB enforces the rule, not just the service):
    uq_secret_workflow_scopes_live   one LIVE row per (secret_id, workflow_id)
                                     pair — the WHERE clause excludes tombstones,
                                     so a replace that re-adds a previously
                                     removed pair never collides with its own
                                     soft-deleted row.

Indexes: secret_id (the gate reads exactly this), tenant_id (the tenant-admin
list), deleted_at (the soft-delete sweep).

The audit-action LABEL (SECRET_SCOPE_UPDATED) ships in the same step: models.AuditAction
is Column(Enum(AuditAction)) with no values_callable, so SQLAlchemy writes the member
NAME and the live `auditaction` PG enum needs that exact UPPERCASE label or every
scope-change audit row is silently dropped (the FX.158 p2a lowercase-label bug class;
tests/test_audit_action_enum_sync.py guards it). ALTER TYPE ... ADD VALUE cannot run
inside a transaction, which is why the whole script runs on an autocommit connection.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzd_fx117_p3b_secret_workflow_scopes.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzd_fx117_p3b_secret_workflow_scopes.py --commit   # apply

NOT applied by the p3b task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "secret_workflow_scopes"


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.117 p3b)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    secret_id INTEGER NOT NULL REFERENCES secrets(id),
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id INTEGER NULL REFERENCES users(id),
    deleted_at TIMESTAMPTZ NULL,
    deleted_by_user_id INTEGER NULL REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live
    ON {_TABLE} (secret_id, workflow_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_secret
    ON {_TABLE} (secret_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant
    ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at
    ON {_TABLE} (deleted_at);
""",
    ),
    (
        "auditaction label for the FX.117 p3b secret-scope change action",
        """
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'SECRET_SCOPE_UPDATED';
""",
    ),
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
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        for stmt in (s.strip() for s in sql.split(";") if s.strip()):
            try:
                cur.execute(stmt)
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {label}: {e}")
                return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "FX.117 p3b ready: secret_workflow_scopes exists and the "
        "SECRET_SCOPE_UPDATED audit label is on the live enum."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
