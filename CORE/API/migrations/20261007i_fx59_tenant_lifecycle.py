"""GSEC FX.59 part 2 (owner decision Q42=A) — tenant contract-end
lifecycle state.

Adds the state-machine columns to `tenants` that the enforcement
dependency (utils/tenant_lifecycle.py) reads on each request, plus the
audit-action enum value that backs
`models.AuditAction.TENANT_LIFECYCLE_STATE_CHANGED`.

tenants:
    lifecycle_state      VARCHAR(20) NOT NULL DEFAULT 'active'
        IN ('active', 'read_only', 'suspended')
    lifecycle_changed_at TIMESTAMPTZ NULL
    lifecycle_reason     VARCHAR(500) NULL

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    TENANT_LIFECYCLE_STATE_CHANGED

Idempotent: ADD COLUMN IF NOT EXISTS + CREATE CONSTRAINT guarded by a
pg_constraint existence check + ALTER TYPE ADD VALUE IF NOT EXISTS.

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction
block, so the whole script runs on an autocommit connection (same
pattern as 20261007a_fx97_provenance_audit_actions.py).

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order, recorded in the append-only
release_steps ledger) — this file sorts after 20261007h.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007i_fx59_tenant_lifecycle.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007i_fx59_tenant_lifecycle.py --commit   # apply

models.py is bind-mounted live into the API container, so the columns
MUST exist before any API process that loads the new models.py runs —
apply immediately after adding them to models.py and BEFORE any API
restart.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "tenants lifecycle state columns",
        """
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS lifecycle_state VARCHAR(20)
        NOT NULL DEFAULT 'active';
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS lifecycle_changed_at TIMESTAMPTZ;
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS lifecycle_reason VARCHAR(500);
""",
    ),
    (
        "tenants.lifecycle_state CHECK constraint",
        """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_tenants_lifecycle_state'
    ) THEN
        ALTER TABLE tenants
            ADD CONSTRAINT ck_tenants_lifecycle_state
            CHECK (lifecycle_state IN ('active', 'read_only', 'suspended'));
    END IF;
END
$$;
""",
    ),
    (
        "auditaction.TENANT_LIFECYCLE_STATE_CHANGED value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'TENANT_LIFECYCLE_STATE_CHANGED';
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
        # whole string per execute — the column batch holds several
        # semicolon-separated statements and the CHECK block is a
        # dollar-quoted DO body, so it must NOT be split on ';'
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.59 tenant lifecycle state schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
