#!/usr/bin/env python3
"""GSEC FX.120 p2a (owner decision Q107=A) — JIT production admin
elevation: the NEW table + the audit-action labels that carry it.

Q107=A: there is NO standing admin on production infrastructure (host
shell, database admin, Vault admin, Docker). An operator who needs one
files an ELEVATION REQUEST: a required reason, one of four scopes, a TTL
(default 2h) that auto-revokes, and a SECOND-PERSON decision — except in
solo mode, where the operator self-approves and the fact is alerted AND
audited. This migration is the DATA half only (p2b builds the approval
service + the scheduler auto-revoke job, p2c the routes).

    privileged_elevation_requests
      id                     BIGSERIAL PRIMARY KEY
      requested_by_user_id   INTEGER NOT NULL  FK users(id)
      scope                  VARCHAR(16) NOT NULL  host_shell|db_admin|
                                                  vault_admin|docker
      reason                 TEXT NOT NULL          REQUIRED (>=10 chars)
      requested_ttl_minutes  INTEGER NOT NULL DEFAULT 120  (1..10080)
      status                 VARCHAR(16) NOT NULL DEFAULT 'pending'
                             pending|approved|denied|active|expired|revoked
      approved_by_user_id    INTEGER NULL          FK users(id)
      self_approved          BOOLEAN NOT NULL DEFAULT false
      approved_at / activated_at / expires_at / revoked_at  TIMESTAMPTZ NULL
      revoke_reason          TEXT NULL
      created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
      deleted_at             TIMESTAMPTZ NULL      soft delete ONLY

Platform scope, not tenant scope (MOD platform staff acting on MOD's own
production infra — same shape as break_glass_use_requests, FX.49), so
there is no tenant_id column.

Constraints (the DB enforces the compliance rules, not just the service):
    ck_..._scope              the four scopes only
    ck_..._status             the six states only
    ck_..._reason             a reason is required and non-blank
    ck_..._ttl                1..10080 minutes (hard ceiling; the service
                              clamps to security.elevation_max_ttl_minutes)
    ck_..._active_clocks      an 'active' row carries activated_at+expires_at
    ck_..._revoked_clocks     a 'revoked' row carries revoked_at+revoke_reason
    ck_..._decision_clocks    anything but 'pending' carries approved_at
    ck_..._self_approved      a self-approval (approved_by == requested_by)
                              MUST set self_approved, so the weekly FX.71
                              access report can never blur a solo-mode
                              self-approval with a colleague's approval.
                              Deliberately NOT the ck_second_approver CHECK
                              FX.163/FX.49 use: solo mode makes a
                              self-approval LEGAL and auditable.

Indexes: scope, status, requested_by_user_id, requested_at-equivalent
(created_at), expires_at, self_approved, deleted_at + the two composites
the p2c list reads order by.

Additive only: nothing existing is altered, dropped, or truncated.
Idempotent: CREATE TABLE IF NOT EXISTS + CREATE ... IF NOT EXISTS.

The six audit-action LABELS (PRIVILEGED_ELEVATION_REQUESTED / APPROVED /
DENIED / ACTIVATED / EXPIRED / REVOKED) ship in the same step: models.AuditAction
is Column(Enum(AuditAction)) with no values_callable, so SQLAlchemy writes the
member NAME and the live `auditaction` PG enum needs those exact uppercase
labels or every elevation audit row is silently dropped (the FX.158 p2a
lowercase-label bug class; tests/test_audit_action_enum_sync.py guards it).
ALTER TYPE ... ADD VALUE cannot run inside a transaction, which is why the
whole script runs on an autocommit connection.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzb_fx120_p2a_privileged_elevation_requests.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzb_fx120_p2a_privileged_elevation_requests.py --commit   # apply

NOT applied by the p2a task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "privileged_elevation_requests"


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.120 p2a)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
    scope VARCHAR(16) NOT NULL,
    reason TEXT NOT NULL,
    requested_ttl_minutes INTEGER NOT NULL DEFAULT 120,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    approved_by_user_id INTEGER NULL REFERENCES users(id),
    self_approved BOOLEAN NOT NULL DEFAULT false,
    approved_at TIMESTAMPTZ NULL,
    activated_at TIMESTAMPTZ NULL,
    expires_at TIMESTAMPTZ NULL,
    revoked_at TIMESTAMPTZ NULL,
    revoke_reason TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_{_TABLE}_scope CHECK (
        scope IN ('host_shell', 'db_admin', 'vault_admin', 'docker')
    ),
    CONSTRAINT ck_{_TABLE}_status CHECK (
        status IN ('pending', 'approved', 'denied', 'active', 'expired',
                   'revoked')
    ),
    CONSTRAINT ck_{_TABLE}_reason CHECK (
        LENGTH(TRIM(reason)) >= 10
    ),
    CONSTRAINT ck_{_TABLE}_ttl CHECK (
        requested_ttl_minutes > 0
        AND requested_ttl_minutes <= 10080
    ),
    CONSTRAINT ck_{_TABLE}_active_clocks CHECK (
        status != 'active'
        OR (activated_at IS NOT NULL AND expires_at IS NOT NULL)
    ),
    CONSTRAINT ck_{_TABLE}_revoked_clocks CHECK (
        status != 'revoked'
        OR (revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)
    ),
    CONSTRAINT ck_{_TABLE}_decision_clocks CHECK (
        status = 'pending' OR status = 'expired'
        OR approved_at IS NOT NULL
    ),
    CONSTRAINT ck_{_TABLE}_self_approved CHECK (
        self_approved = true
        OR approved_by_user_id != requested_by_user_id
    )
);
""",
    ),
    (
        f"{_TABLE} indexes",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_scope ON {_TABLE} (scope);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_status ON {_TABLE} (status);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_requested_by_user
    ON {_TABLE} (requested_by_user_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_created_at ON {_TABLE} (created_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_expires_at ON {_TABLE} (expires_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_self_approved
    ON {_TABLE} (self_approved);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at ON {_TABLE} (deleted_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_requested_status
    ON {_TABLE} (requested_by_user_id, status);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_scope_status
    ON {_TABLE} (scope, status);
""",
    ),
    (
        "auditaction labels for the six FX.120 elevation actions",
        """
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'PRIVILEGED_ELEVATION_REQUESTED';
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'PRIVILEGED_ELEVATION_APPROVED';
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'PRIVILEGED_ELEVATION_DENIED';
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'PRIVILEGED_ELEVATION_ACTIVATED';
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'PRIVILEGED_ELEVATION_EXPIRED';
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'PRIVILEGED_ELEVATION_REVOKED';
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
        "FX.120 p2a ready: privileged_elevation_requests exists and the six "
        "PRIVILEGED_ELEVATION_* audit labels are on the live enum."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
