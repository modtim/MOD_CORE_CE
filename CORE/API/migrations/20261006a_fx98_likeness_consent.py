"""FX.98 part 1 (owner decision Q84=A) — add the `likeness_consents` table.

Likeness consent register: one row per real person whose likeness a
tenant has consent to replicate (nodes tagged 'likeness' — see
services/ai_prohibited_use.py). A run of a likeness-tagged node on a real
person requires a linked, non-expired, non-revoked, in-scope consent
record; a synthetic attestation is recorded instead when no real person
is involved.

Columns and constraints mirror models.LikenessConsent exactly:
* SOFT-DELETE ONLY (ISO 27001 — no hard deletes): `revoked_at` marks a
  revoked consent, `deleted_at` marks a register soft-delete; the row
  always stays for the compliance record.
* The audit trail lives in `permission_audit_logs`
  (LIKENESS_CONSENT_CREATED / LIKENESS_CONSENT_REVOKED /
  LIKENESS_CONSENT_DELETED — see services/likeness_consent.py), this
  table holds none.

The repo's migration runner is `tools/mod_migrate.py`, not Alembic:
steps are discovered as `*.py` files in this directory and run in
filename order, each recorded in the append-only `release_steps`
ledger. Naming convention `YYYYMMDD[_letter].slug.py` — this file sorts
before 20261006t.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261006a_fx98_likeness_consent.py            # dry run
    docker exec my_fastapi_app python3 \\
        migrations/20261006a_fx98_likeness_consent.py --commit   # apply

Additive only: CREATE TABLE/INDEX IF NOT EXISTS — nothing is dropped
or truncated.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "likeness_consents"

TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    person_name VARCHAR(255) NOT NULL,
    scope TEXT,
    expires_at TIMESTAMP WITH TIME ZONE,
    consent_doc_file_id INTEGER,
    contract_ref VARCHAR(255),
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    revoked_at TIMESTAMP WITH TIME ZONE,
    deleted_at TIMESTAMP WITH TIME ZONE
);
"""

INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS ix_likeness_consents_tenant_id
    ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_likeness_consents_expires_at
    ON {_TABLE} (expires_at);
CREATE INDEX IF NOT EXISTS ix_likeness_consents_created_at
    ON {_TABLE} (created_at);
CREATE INDEX IF NOT EXISTS ix_likeness_consents_revoked_at
    ON {_TABLE} (revoked_at);
CREATE INDEX IF NOT EXISTS ix_likeness_consents_deleted_at
    ON {_TABLE} (deleted_at);
CREATE INDEX IF NOT EXISTS ix_likeness_consents_tenant_expiry
    ON {_TABLE} (tenant_id, expires_at);
"""

# AuditAction is a PG enum (`auditaction`) storing member NAMES
# (uppercase). Each ADD VALUE on a type already in use must run in
# autocommit mode — this script runs autocommit throughout.
ENUM_STATEMENTS = [
    ("auditaction += LIKENESS_CONSENT_CREATED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
     "'LIKENESS_CONSENT_CREATED'"),
    ("auditaction += LIKENESS_CONSENT_REVOKED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
     "'LIKENESS_CONSENT_REVOKED'"),
    ("auditaction += LIKENESS_CONSENT_DELETED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
     "'LIKENESS_CONSENT_DELETED'"),
    ("auditaction += LIKENESS_RUN_GATED",
     "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS "
     "'LIKENESS_RUN_GATED'"),
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
