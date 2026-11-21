"""GSEC FX.95 p4a (owner decision Q81=A) — publisher identity/company
verification behind the marketplace Verified badge.

Creates `publisher_verifications` (models.PublisherVerification), the
per-publisher-tenant record MOD staff use to record that a marketplace
publisher's identity/company was checked (services/publisher_verification.py):

    publisher_tenant_id  INTEGER NOT NULL UNIQUE  (FK tenants.id)
    status               VARCHAR(20) NOT NULL      unverified|pending|
                                                    verified|revoked
    method               VARCHAR(32) NULL          domain_dns|business_registry|
                                                    manual_documents|other
    evidence_note        TEXT NULL                 publisher's capped note
    decision_note        TEXT NULL                 staff approve/reject note
    requested_at         TIMESTAMPTZ NULL
    requested_by_user_id INTEGER NULL              users.id FK
    verified_at          TIMESTAMPTZ NULL
    verified_by          INTEGER NULL              users.id FK (staff)
    rejected_at          TIMESTAMPTZ NULL
    rejected_by          INTEGER NULL              users.id FK (staff)
    revoked_at           TIMESTAMPTZ NULL
    revoked_by           INTEGER NULL              users.id FK (staff)
    revoked_reason       TEXT NULL
    created_at / updated_at TIMESTAMPTZ NOT NULL
    deleted_at           TIMESTAMPTZ NULL          SoftDeleteMixin

Additive only: a NEW table, nothing existing is touched, and no row is ever
hard-deleted (ISO 27001 — revoke is a state change, the row and its audit
history stay). The CHECK constraint mirrors the service's allowed states.

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    PUBLISHER_VERIFICATION_REQUESTED / _APPROVED / _REJECTED / _REVOKED
One append-only row per transition (identifiers/method/notes only, never the
evidence documents).

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS +
ALTER TYPE ADD VALUE IF NOT EXISTS. `ALTER TYPE auditaction ADD VALUE` cannot
run inside a transaction block, so the script runs on an autocommit connection
(same pattern as 20261007p_fx95_listing_review.py).

Dry run (default) PRINTS the SQL and exits 0 without touching the database.
Apply with --commit (the lead applies; this task does not):
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zy_fx95_p4a_publisher_verification.py
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zy_fx95_p4a_publisher_verification.py --commit

models.py is bind-mounted live into the API container, so the table + enum
values must exist before any FX.95 p4a code path writes to them.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "publisher_verifications"

_STATEMENTS = [
    (
        f"{_TABLE} (FX.95 p4a publisher verification record)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id SERIAL PRIMARY KEY,
    publisher_tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL DEFAULT 'unverified',
    method VARCHAR(32) NULL,
    evidence_note TEXT NULL,
    decision_note TEXT NULL,
    requested_at TIMESTAMPTZ NULL,
    requested_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    verified_at TIMESTAMPTZ NULL,
    verified_by INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    rejected_at TIMESTAMPTZ NULL,
    rejected_by INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    revoked_at TIMESTAMPTZ NULL,
    revoked_by INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    revoked_reason TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_{_TABLE}_status CHECK (
        status IN ('unverified', 'pending', 'verified', 'revoked'))
);
""",
    ),
    (
        f"unique constraint on {_TABLE} (one record per publisher tenant)",
        f"""
DO $$
BEGIN
    IF to_regclass('{_TABLE}') IS NOT NULL AND EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = '{_TABLE}'::regclass
          AND c.conname = 'uq_{_TABLE}_publisher_tenant_id'
    ) THEN
        RAISE NOTICE 'uq_{_TABLE}_publisher_tenant_id already present - skipping';
        RETURN;
    END IF;
    ALTER TABLE {_TABLE}
        ADD CONSTRAINT uq_{_TABLE}_publisher_tenant_id
        UNIQUE (publisher_tenant_id);
END
$$;
""",
    ),
    (
        f"indexes on {_TABLE} (names match models.py index=True exactly — one "
        "definition per column, so conftest create_all and this migration agree)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_publisher_tenant_id ON {_TABLE} (publisher_tenant_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_status ON {_TABLE} (status);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at ON {_TABLE} (deleted_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_requested_by_user_id ON {_TABLE} (requested_by_user_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_verified_by ON {_TABLE} (verified_by);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_rejected_by ON {_TABLE} (rejected_by);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_revoked_by ON {_TABLE} (revoked_by);
""",
    ),
]

_ENUM_VALUES = [
    "PUBLISHER_VERIFICATION_REQUESTED",
    "PUBLISHER_VERIFICATION_APPROVED",
    "PUBLISHER_VERIFICATION_REJECTED",
    "PUBLISHER_VERIFICATION_REVOKED",
]


def _split_statements(sql: str) -> list[str]:
    """Split on ';' except inside a DO $$ ... $$ block (the idempotency
    guard for the unique constraint is one such block)."""
    out: list[str] = []
    buf: list[str] = []
    for chunk in sql.split(";"):
        buf.append(chunk)
        joined = ";".join(buf)  # BUG-1027w-d: re-join with ';' — "".join dropped every ';' inside the DO $$..$$ guard
        if joined.count("$$") % 2 == 0:
            stmt = joined.strip()
            if stmt:
                out.append(stmt)
            buf = []
    tail = ";".join(buf).strip()  # BUG-1027w-d: keep ';' inside DO $$..$$ blocks
    if tail:
        out.append(tail)
    return out


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

    statements = list(_STATEMENTS)
    for value in _ENUM_VALUES:
        statements.append((
            f"auditaction {value}",
            f"ALTER TYPE auditaction\n    ADD VALUE IF NOT EXISTS '{value}';",
        ))

    if not args.commit:
        for label, sql in statements:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in statements:
        for stmt in _split_statements(sql):
            cur.execute(stmt)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print("FX.95 p4a publisher verification schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
