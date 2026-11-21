"""GSEC FX.85 part 1a (owner decision Q70=A) — subprocessor change log.

Owner commitment: customers get at least 30 days' notice before a
subprocessor is added / materially changed (row-9 note in
docs/security-audit/legal-drafts/subprocessors.md). This table records
every announced change (add / remove / replace / purpose_change) so
the change log and the customer-facing list (kept in the docs —
docs-site/docs/legal/subprocessors.md) never drift apart. AI providers
a tenant toggles itself are exempt from the notice (Q70 — the
per-tenant opt-in gate is the notice) and are flagged per row.

subprocessor_changes (NEW): one row per announced subprocessor change.
    id                              BIGSERIAL PK
    subprocessor_name   VARCHAR(255) NOT NULL
    change_type         VARCHAR(20)  NOT NULL
        CHECK IN ('add', 'remove', 'replace', 'purpose_change')
    description         TEXT NULL
    data_categories     JSONB NULL    (dict or list of strings)
    region              VARCHAR(120) NULL
    announced_at        TIMESTAMPTZ NOT NULL
    effective_at        TIMESTAMPTZ NULL
        default = announced_at + subprocessor.notice_days (30); the
        nullable override carries an earlier emergency effective date
    is_emergency        BOOLEAN NOT NULL DEFAULT FALSE
    is_ai_provider_tenant_toggled BOOLEAN NOT NULL DEFAULT FALSE
        (Q70 — tenant-toggled AI providers are notice-exempt)
    created_by_user_id  INTEGER NULL  users.id FK
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()

Append-only (ISO 27001 — no hard deletes): no deleted_at column, no
UPDATE/DELETE path anywhere; the service only records and lists.

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. Dry run (default) PRINTS
the SQL and exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zc_fx85_subprocessor_changes.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zc_fx85_subprocessor_changes.py --commit   # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order) — this file sorts after
20261007zb. models.py is bind-mounted live into the API container, so
the table must exist before any FX.85 part-1a code path writes to it —
apply immediately after adding the model to models.py. NOT applied by
this task (model/migration/service/router/tests only, p1a) — do not
run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "subprocessor_changes table",
        """
CREATE TABLE IF NOT EXISTS subprocessor_changes (
    id BIGSERIAL PRIMARY KEY,
    subprocessor_name VARCHAR(255) NOT NULL,
    change_type VARCHAR(20) NOT NULL,
    description TEXT NULL,
    data_categories JSONB NULL,
    region VARCHAR(120) NULL,
    announced_at TIMESTAMPTZ NOT NULL,
    effective_at TIMESTAMPTZ NULL,
    is_emergency BOOLEAN NOT NULL DEFAULT FALSE,
    is_ai_provider_tenant_toggled BOOLEAN NOT NULL DEFAULT FALSE,
    created_by_user_id INTEGER NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_subprocessor_changes_change_type CHECK (
        change_type IN ('add', 'remove', 'replace', 'purpose_change')
    )
);
""",
    ),
    (
        "subprocessor_changes indexes",
        """
CREATE INDEX IF NOT EXISTS ix_subprocessor_changes_subprocessor_name
    ON subprocessor_changes (subprocessor_name);
CREATE INDEX IF NOT EXISTS ix_subprocessor_changes_change_type
    ON subprocessor_changes (change_type);
CREATE INDEX IF NOT EXISTS ix_subprocessor_changes_announced_at
    ON subprocessor_changes (announced_at);
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
    print("FX.85 p1a subprocessor change log schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
