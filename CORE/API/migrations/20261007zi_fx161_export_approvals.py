"""GSEC FX.161.p2a (owner decision Q148=A, DLP egress) — approval
workflow table for FULL tenant exports under a regulated compliance
profile (models.ExportApprovalRequest / services/export_approval.py).

During the regulated-profile (cmmc / fedramp_high_il5 by default, see
dlp.export_approval_profiles) period a tenant cannot run the FX.59
full export directly: POST /v1/tenant/export creates a PENDING row
here, a DIFFERENT tenant admin approves or rejects it, and only an
approved, unexpired row lets the export run — exactly once
(``used_at`` stamped on consumption). The row is never hard-deleted
(ISO 27001): removal is a ``deleted_at`` stamp.

export_approval_requests (NEW):
    id                  BIGSERIAL PK
    tenant_id           INTEGER NOT NULL REFERENCES tenants(id)
    requested_by        INTEGER NOT NULL REFERENCES users(id)
    scope               VARCHAR(64) NOT NULL     ("full_tenant_export")
    reason              TEXT NULL
    status              VARCHAR(32) NOT NULL DEFAULT 'pending'
                        (pending | approved | rejected | expired, CHECK)
    approver_user_id    INTEGER NULL REFERENCES users(id)
    decision_reason     TEXT NULL
    decided_at          TIMESTAMPTZ NULL
    expires_at          TIMESTAMPTZ NULL
    used_at             TIMESTAMPTZ NULL         (one-shot consumption)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    updated_at          TIMESTAMPTZ NULL
    deleted_at          TIMESTAMPTZ NULL        (soft delete only)
    deleted_by          INTEGER NULL REFERENCES users(id)

Only EXISTING auditaction values are used for the audit rows
(TENANT_EXPORT_REQUESTED / SECURITY_INCIDENT_CREATED), so there is no
ALTER TYPE here. Dry run (default) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zi_fx161_export_approvals.py           # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zi_fx161_export_approvals.py --commit  # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order) — this file sorts after
20261007zh (FX.78 p4 BYO-IdP secret ref). NOT applied by this task
(model/migration/service/router/tests only) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "export_approval_requests table",
        """
CREATE TABLE IF NOT EXISTS export_approval_requests (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    requested_by INTEGER NOT NULL REFERENCES users(id),
    scope VARCHAR(64) NOT NULL,
    reason TEXT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    approver_user_id INTEGER NULL REFERENCES users(id),
    decision_reason TEXT NULL,
    decided_at TIMESTAMPTZ NULL,
    expires_at TIMESTAMPTZ NULL,
    used_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL,
    deleted_by INTEGER NULL REFERENCES users(id),
    CONSTRAINT ck_export_approval_requests_status
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired'))
);
""",
    ),
    (
        "export_approval_requests indexes",
        """
CREATE INDEX IF NOT EXISTS ix_export_approval_requests_tenant_id
    ON export_approval_requests (tenant_id);
CREATE INDEX IF NOT EXISTS ix_export_approval_requests_requested_by
    ON export_approval_requests (requested_by);
CREATE INDEX IF NOT EXISTS ix_export_approval_requests_status
    ON export_approval_requests (status);
CREATE INDEX IF NOT EXISTS ix_export_approval_requests_approver_user_id
    ON export_approval_requests (approver_user_id);
CREATE INDEX IF NOT EXISTS ix_export_approval_requests_expires_at
    ON export_approval_requests (expires_at);
CREATE INDEX IF NOT EXISTS ix_export_approval_requests_tenant_created
    ON export_approval_requests (tenant_id, created_at);
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
        # one statement per semicolon — the batch may hold several
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
