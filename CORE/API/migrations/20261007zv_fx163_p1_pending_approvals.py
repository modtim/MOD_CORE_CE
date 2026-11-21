"""GSEC FX.163 p1 (owner decision Q150=A) — segregation of duties /
maker-checker: the pending-approval queue for sensitive tenant actions.

FX.163 p1 is the DATA half of the maker-checker vertical: the table the
approval service (p2) writes and the Dashboard page (p3) lists. A tenant
action that can quietly widen access — a role grant, a FULL tenant export,
a BYOK revoke, a data-marking clear — is recorded here when the tenant
setting ``approvals.require_second_approver`` is ON (or an active
compliance profile forces it ON; see services/approvals.py). The action
itself does NOT run until a DIFFERENT tenant admin approves this row.

    pending_approvals:
        id BIGSERIAL PRIMARY KEY
        tenant_id           INTEGER NOT NULL REFERENCES tenants(id) (indexed)
        action              VARCHAR(64) NOT NULL (role_grant | full_export
                            | byok_revoke | data_marking_clear — the
                            service validates the key, the DB does not
                            enumerate it so p2 can add actions)
        payload             JSONB NOT NULL DEFAULT '{}' (identifiers only)
        payload_sha256      VARCHAR(64) NOT NULL (digest of the canonical
                            JSON of payload; the service re-hashes at
                            execution and refuses on a mismatch)
        requested_by_user_id INTEGER NOT NULL REFERENCES users(id)
        requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        approved_by_user_id INTEGER NULL REFERENCES users(id)
        decided_at          TIMESTAMPTZ NULL
        status              VARCHAR(32) NOT NULL DEFAULT 'pending'
                            (pending|approved|rejected|expired|cancelled)
        expires_at          TIMESTAMPTZ NULL
        reason / decision_note TEXT NULL
        deleted_at          TIMESTAMPTZ NULL (ISO 27001: soft delete ONLY)

Constraints:
    ck_pending_approvals_status            status IN (the five states)
    ck_pending_approvals_second_approver   approved_by_user_id IS NULL OR
                                           approved_by_user_id !=
                                           requested_by_user_id
    The maker-checker rule is enforced BY THE DB, not only by the service:
    a self-approval cannot be written even by a raw INSERT, so a future
    writer that forgets the service check still cannot approve its own
    request.

Indexes: action, requested_at, status, deleted_at, plus the tenant-scoped
composites the queue list (p3) and the pending-count reads order by.

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. Dry run (default) PRINTS the
SQL and exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zv_fx163_p1_pending_approvals.py           # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zv_fx163_p1_pending_approvals.py --commit  # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in this
directory, run in filename order) — this file sorts after 20261007zu (FX.73
p4c CE included). NOT applied by this task (model/migration/service/tests
only) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "pending_approvals"

_STATEMENTS = [
    (
        "pending_approvals table",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    action VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    payload_sha256 VARCHAR(64) NOT NULL,
    requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_by_user_id INTEGER NULL REFERENCES users(id),
    decided_at TIMESTAMPTZ NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    expires_at TIMESTAMPTZ NULL,
    reason TEXT NULL,
    decision_note TEXT NULL,
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_{_TABLE}_status CHECK (
        status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')
    ),
    CONSTRAINT ck_{_TABLE}_second_approver CHECK (
        approved_by_user_id IS NULL
        OR approved_by_user_id != requested_by_user_id
    )
);
""",
    ),
    (
        f"{_TABLE} indexes",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_action ON {_TABLE} (action);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_requested_at ON {_TABLE} (requested_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_status ON {_TABLE} (status);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at ON {_TABLE} (deleted_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_requested
    ON {_TABLE} (tenant_id, requested_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_status
    ON {_TABLE} (tenant_id, status);
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
        try:
            cur.execute(sql)
            print(f"OK   {label}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
