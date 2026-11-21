"""GSEC FX.153 part 2 (owner decision Q140=B) — add the `staff_records` table.

In-app personnel security-policy staff register
(docs/isms/21-personnel-security-policy.md, "In-app records"): one row
per person on the MOD payroll/security boundary — person name, email,
role, staff_type (employee | contractor), NDA/AUP signed dates,
training-completed date, gov-cleared + US-person flags, and the leaver
flow (leaver_at + leaver_checklist JSONB mapping the policy's 7 systems
— keycloak, vault, harbor, git, tailnet, cloud_accounts, colo_access_list
— to a removed_at per system as each is closed).

Columns mirror models.StaffRecord exactly:
* SOFT-DELETE ONLY (ISO 27001 — no hard deletes): the register's delete
  stamps `deleted_at`; the row always stays (policy "Records retention":
  staff-register rows kept >= 3 years).
* The audit trail lives in `permission_audit_logs`
  (STAFF_RECORD_CREATED / STAFF_RECORD_UPDATED / STAFF_RECORD_DELETED /
  STAFF_LEAVER_CHECKLIST_STARTED / STAFF_LEAVER_SYSTEM_REMOVED — see
  services/staff_register.py); this table holds none.

The repo's migration runner is `tools/mod_migrate.py`, not Alembic:
steps are discovered as `*.py` files in this directory and run in
filename order, each recorded in the append-only `release_steps`
ledger. Naming convention `YYYYMMDD[_letter].slug.py` — this file sorts
after 20261007a.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261007b_fx153_staff_register.py            # dry run
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261007b_fx153_staff_register.py --commit   # apply

Additive only: CREATE TABLE/INDEX IF NOT EXISTS + enum ADD VALUE IF
NOT EXISTS — nothing is dropped or truncated.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "staff_records"

TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER,
    person_name VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    role VARCHAR(255),
    staff_type VARCHAR(32) NOT NULL DEFAULT 'employee',
    nda_signed_at TIMESTAMP WITH TIME ZONE,
    aup_signed_at TIMESTAMP WITH TIME ZONE,
    training_completed_at TIMESTAMP WITH TIME ZONE,
    gov_cleared BOOLEAN NOT NULL DEFAULT FALSE,
    us_person_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    leaver_at TIMESTAMP WITH TIME ZONE,
    leaver_checklist JSONB,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE,
    deleted_at TIMESTAMP WITH TIME ZONE
);
"""

INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS ix_staff_records_tenant_id
    ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_staff_records_person_name
    ON {_TABLE} (person_name);
CREATE INDEX IF NOT EXISTS ix_staff_records_staff_type
    ON {_TABLE} (staff_type);
CREATE INDEX IF NOT EXISTS ix_staff_records_training_completed_at
    ON {_TABLE} (training_completed_at);
CREATE INDEX IF NOT EXISTS ix_staff_records_gov_cleared
    ON {_TABLE} (gov_cleared);
CREATE INDEX IF NOT EXISTS ix_staff_records_leaver_at
    ON {_TABLE} (leaver_at);
CREATE INDEX IF NOT EXISTS ix_staff_records_created_at
    ON {_TABLE} (created_at);
CREATE INDEX IF NOT EXISTS ix_staff_records_deleted_at
    ON {_TABLE} (deleted_at);
"""

# AuditAction is a PG enum (`auditaction`) storing member NAMES
# (uppercase). Each ADD VALUE on a type already in use must run in
# autocommit mode — this script runs autocommit throughout.
ENUM_STATEMENTS = [
    ("auditaction += STAFF_RECORD_CREATED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
     "'STAFF_RECORD_CREATED'"),
    ("auditaction += STAFF_RECORD_UPDATED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
     "'STAFF_RECORD_UPDATED'"),
    ("auditaction += STAFF_RECORD_DELETED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
     "'STAFF_RECORD_DELETED'"),
    ("auditaction += STAFF_LEAVER_CHECKLIST_STARTED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
     "'STAFF_LEAVER_CHECKLIST_STARTED'"),
    ("auditaction += STAFF_LEAVER_SYSTEM_REMOVED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
     "'STAFF_LEAVER_SYSTEM_REMOVED'"),
]

_STATEMENTS = [
    (f"{_TABLE} table", TABLE_SQL),
    (f"indexes on {_TABLE}", INDEX_SQL),
    *ENUM_STATEMENTS,
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
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print(f"{_TABLE} ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
