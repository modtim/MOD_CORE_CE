"""GSEC FX.158 p2a (owner decision Q145=A) — security advisories (CVE IDs).

security_advisories: platform-admin-authored advisories backing the public
trust-page feed (routers/public_security_advisories.py). One row per advisory:

    advisory_id        VARCHAR(40) NOT NULL UNIQUE  (e.g. MOD-SA-2026-001)
    title              VARCHAR(200) NOT NULL
    severity           VARCHAR(16) NOT NULL  (critical|high|medium|low)
    cve_ids            JSONB NOT NULL DEFAULT '[]'  (each entry validated
                       against CVE-\\d{4}-\\d{4,} in the service layer)
    affected_versions  JSONB NOT NULL DEFAULT '[]'
    fixed_in_version   VARCHAR(120) NULL
    summary            TEXT NOT NULL
    published_at       TIMESTAMPTZ NULL  (NULL = DRAFT: admin view only,
                       never served on the public feed)
    created_at / updated_at, created_by / published_by (users.id)
    deleted_at         TIMESTAMPTZ NULL  (ISO 27001: soft delete ONLY)

Indexes: severity, published_at, created_at, deleted_at, plus the
published-desc composite the admin list and the public feed order by.

Audit actions added for the write paths (append-only, identifiers only):
SECURITY_ADVISORY_CREATED / _UPDATED / _PUBLISHED / _DELETED.
ALTER TYPE ... ADD VALUE IF NOT EXISTS cannot run inside a transaction, so
each statement is executed with autocommit (same shape as
20261007za_fx76_nda_requests.py).

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. Dry run (default) PRINTS the
SQL and exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zo_fx158_p2a_advisories.py           # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zo_fx158_p2a_advisories.py --commit  # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in this
directory, run in filename order) — this file sorts after 20261007zh (FX.78
p4 IdP credential reference columns). NOT applied by this task (model/
migration/service/router/tests only) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "security_advisories"

_STATEMENTS = [
    (
        "security_advisories table",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    advisory_id VARCHAR(40) NOT NULL UNIQUE,
    title VARCHAR(200) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    cve_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    affected_versions JSONB NOT NULL DEFAULT '[]'::jsonb,
    fixed_in_version VARCHAR(120) NULL,
    summary TEXT NOT NULL,
    published_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NULL,
    created_by INTEGER NULL REFERENCES users(id),
    published_by INTEGER NULL REFERENCES users(id),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_{_TABLE}_severity CHECK (
        severity IN ('critical', 'high', 'medium', 'low')
    )
);
""",
    ),
    (
        f"{_TABLE} indexes",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_severity ON {_TABLE} (severity);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_published_at ON {_TABLE} (published_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_created_at ON {_TABLE} (created_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at ON {_TABLE} (deleted_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_published_desc
    ON {_TABLE} (published_at DESC, created_at DESC);
""",
    ),
    (
        "auditaction security-advisory values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SECURITY_ADVISORY_CREATED';
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SECURITY_ADVISORY_UPDATED';
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SECURITY_ADVISORY_PUBLISHED';
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'SECURITY_ADVISORY_DELETED';
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
