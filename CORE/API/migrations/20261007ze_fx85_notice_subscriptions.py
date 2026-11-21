"""GSEC FX.85 p1b (owner decision Q70=A) — subprocessor change notice
subscriptions (tenant-admin email opt-in).

subprocessor_notice_subscriptions (NEW): one row per (tenant, email) that
has opted in to being emailed when a subprocessor change is announced
(FX.85 p2 fans the notices out to services/subprocessor_changes.py's
active_recipients()).
    id                      BIGSERIAL PK
    tenant_id       INTEGER NOT NULL  tenants.id FK (indexed)
    email           VARCHAR(255) NOT NULL
    subscribed_by_user_id INTEGER NULL  users.id FK (the tenant admin who
        subscribed it)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    unsubscribed_at TIMESTAMPTZ NULL
        NULL = active. Unsubscribing STAMPS this column; the row is never
        deleted (ISO 27001 — no hard deletes), and a re-subscribe is a NEW
        row, never a resurrection of the old one.

Active uniqueness — one active row per (tenant, email, case-insensitive)
— is enforced in the service (services/subprocessor_changes.py::subscribe)
AND by the partial unique index below (WHERE unsubscribed_at IS NULL), so
two live API processes can never race into a duplicate active row.

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. Dry run (default) PRINTS the
SQL and exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007ze_fx85_notice_subscriptions.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007ze_fx85_notice_subscriptions.py --commit   # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in this
directory, run in filename order) — this file sorts after
20261007zd (FX.88 MFA recovery). models.py is bind-mounted live into the
API container, so the table must exist before any FX.85 p1b code path
writes to it. NOT applied by this task (model/migration/service/router/
tests only) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "subprocessor_notice_subscriptions table",
        """
CREATE TABLE IF NOT EXISTS subprocessor_notice_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    email VARCHAR(255) NOT NULL,
    subscribed_by_user_id INTEGER NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    unsubscribed_at TIMESTAMPTZ NULL
);
""",
    ),
    (
        "subprocessor_notice_subscriptions indexes",
        """
CREATE INDEX IF NOT EXISTS ix_subprocessor_notice_subscriptions_tenant_id
    ON subprocessor_notice_subscriptions (tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS
    uq_subprocessor_notice_subscriptions_active
    ON subprocessor_notice_subscriptions (tenant_id, lower(email))
    WHERE unsubscribed_at IS NULL;
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
    print("FX.85 p1b notice subscription schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
