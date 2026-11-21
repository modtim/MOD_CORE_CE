#!/usr/bin/env python3
"""GSEC FX.117 p4a/p4b — per-secret expiry RECORD (models.SecretExpiry).

FX.117 (owner Q104=A) ships the customer secret store. p3b gave a tenant admin
a per-secret allowed-workflows scope as a NEW table (`secret_workflow_scopes`)
mirroring the p2 `Secret.allowed_workflow_ids` bag; p4 does the same for
expiry: the expiry date an admin sets lives in `secret_expiries`, and the p2
`Secret.expires_at` bag stays as the admin-facing view this table mirrors.

NEW TABLE ONLY (models.py is bind-mounted live and shared by every lane: NO
column was added to the existing `secrets` table).

SOFT-DELETE ONLY (ISO 27001 — clearing an expiry soft-deletes the live row and
stamps `deleted_at`; nothing is ever hard-deleted), and the secret_id FK
carries NO `ON DELETE CASCADE` clause: deleting a secret must not drop the
record of the expiry it once owned.

    secret_expiries
      id                  BIGSERIAL PRIMARY KEY
      tenant_id           INTEGER NOT NULL  FK tenants(id)   owner tenant
      secret_id           INTEGER NOT NULL  FK secrets(id)   (no cascade)
      expires_at          TIMESTAMPTZ NOT NULL  the expiry date
      last_reminded_at    TIMESTAMPTZ NULL    p4b reminder dedupe record
      created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
      created_by_user_id  INTEGER NULL      FK users(id)
      deleted_at          TIMESTAMPTZ NULL  soft delete ONLY
      deleted_by_user_id  INTEGER NULL      FK users(id)

Constraints (the DB enforces the rule, not just the service):
    uq_secret_expiries_live   one LIVE row per secret_id — the WHERE clause
                              excludes tombstones, so moving an expiry
                              forward (soft-delete + insert) never collides
                              with its own soft-deleted row.

Indexes: secret_id (the enforcement read at run-time injection), tenant_id
(the tenant-admin list), expires_at (the p4b reminder sweep's range scan).

The audit-action LABELs (SECRET_EXPIRY_SET / SECRET_EXPIRY_REMINDER_SENT) ship
in the same step: models.AuditAction is Column(Enum(AuditAction)) with no
values_callable, so SQLAlchemy writes the member NAME and the live `auditaction`
PG enum needs that exact UPPERCASE label or every expiry audit row is silently
dropped (the FX.158 p2a lowercase-label bug class; tests/test_audit_action_enum_sync.py
guards it). ALTER TYPE ... ADD VALUE cannot run inside a transaction, which is
why the whole script runs on an autocommit connection.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzf_fx117_p4_secret_expiries.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzf_fx117_p4_secret_expiries.py --commit   # apply

NOT applied by the p4 task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "secret_expiries"


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.117 p4a)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    secret_id INTEGER NOT NULL REFERENCES secrets(id),
    expires_at TIMESTAMPTZ NOT NULL,
    last_reminded_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id INTEGER NULL REFERENCES users(id),
    deleted_at TIMESTAMPTZ NULL,
    deleted_by_user_id INTEGER NULL REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live
    ON {_TABLE} (secret_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_secret
    ON {_TABLE} (secret_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant
    ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_expires_at
    ON {_TABLE} (expires_at);
""",
    ),
    (
        "auditaction labels for the FX.117 p4 secret-expiry actions",
        """
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'SECRET_EXPIRY_SET';
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'SECRET_EXPIRY_REMINDER_SENT';
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
        "FX.117 p4 ready: secret_expiries exists and the SECRET_EXPIRY_SET / "
        "SECRET_EXPIRY_REMINDER_SENT audit labels are on the live enum."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
