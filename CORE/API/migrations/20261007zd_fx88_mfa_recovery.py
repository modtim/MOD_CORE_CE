"""GSEC FX.88 p3 — last-admin MFA recovery workflow.

mfa_recovery_requests (NEW): a request to reset MFA for the sole remaining
active admin of a tenant, when that admin is locked out and nobody else in
the tenant can perform the FX.88 p2a tenant-admin reset.
    id                          BIGINT PK
    tenant_id                   INTEGER NOT NULL      tenants.id FK
    target_user_id              INTEGER NOT NULL      users.id FK
    requester_email             VARCHAR(255) NOT NULL
    security_contact_email      VARCHAR(255) NULL     snapshot at creation
    reason                      TEXT NULL
    status                      VARCHAR(20) NOT NULL DEFAULT 'requested'
                                 requested|callback_verified|approved|
                                 rejected|completed|expired
    callback_verified_by_user_id INTEGER NULL         users.id FK
    callback_verified_at        TIMESTAMPTZ NULL
    callback_note                TEXT NULL
    approved_by_user_id         INTEGER NULL         users.id FK
    approved_at                  TIMESTAMPTZ NULL
    rejected_by_user_id         INTEGER NULL         users.id FK
    rejected_at                  TIMESTAMPTZ NULL
    rejection_reason              TEXT NULL
    completed_at                  TIMESTAMPTZ NULL
    expires_at                    TIMESTAMPTZ NOT NULL
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    deleted_at                    TIMESTAMPTZ NULL     soft-delete (ISO 27001)

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. Dry run (default) PRINTS the
SQL and exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zd_fx88_mfa_recovery.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zd_fx88_mfa_recovery.py --commit   # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in this
directory, run in filename order) — this file sorts after 20261007zc.
models.py is bind-mounted live into the API container, so the table must
exist before any FX.88 p3 code path writes to it — apply immediately after
adding the model to models.py. NOT applied by this task (model/migration/
tests only) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "mfa_recovery_requests table",
        """
CREATE TABLE IF NOT EXISTS mfa_recovery_requests (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    target_user_id INTEGER NOT NULL REFERENCES users(id),
    requester_email VARCHAR(255) NOT NULL,
    security_contact_email VARCHAR(255) NULL,
    reason TEXT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'requested',
    callback_verified_by_user_id INTEGER NULL REFERENCES users(id),
    callback_verified_at TIMESTAMPTZ NULL,
    callback_note TEXT NULL,
    approved_by_user_id INTEGER NULL REFERENCES users(id),
    approved_at TIMESTAMPTZ NULL,
    rejected_by_user_id INTEGER NULL REFERENCES users(id),
    rejected_at TIMESTAMPTZ NULL,
    rejection_reason TEXT NULL,
    completed_at TIMESTAMPTZ NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_mfa_recovery_requests_status CHECK (
        status IN ('requested', 'callback_verified', 'approved', 'rejected',
                  'completed', 'expired')
    )
);
""",
    ),
    (
        "mfa_recovery_requests indexes",
        """
CREATE INDEX IF NOT EXISTS ix_mfa_recovery_requests_tenant_id
    ON mfa_recovery_requests (tenant_id);
CREATE INDEX IF NOT EXISTS ix_mfa_recovery_requests_target_user_id
    ON mfa_recovery_requests (target_user_id);
CREATE INDEX IF NOT EXISTS ix_mfa_recovery_requests_status
    ON mfa_recovery_requests (status);
CREATE INDEX IF NOT EXISTS ix_mfa_recovery_requests_deleted_at
    ON mfa_recovery_requests (deleted_at);
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
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.88 p3 mfa_recovery_requests schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
