"""GSEC FX.85 p2a (owner decision Q70=A) — subprocessor change notice
delivery log (idempotent fan-out).

subprocessor_notice_deliveries (NEW): one row per DELIVERY ATTEMPT of a
published subprocessor change to one recipient (tenant, email) — the
append-only idempotency log behind
services/subprocessor_changes.py::send_change_notices and the daily
'fx85_subprocessor_notices' scheduler tick.
    id        BIGSERIAL PK
    change_id BIGINT NOT NULL  subprocessor_changes.id FK (indexed)
    tenant_id INTEGER NOT NULL  tenants.id FK (indexed)
    email     VARCHAR(255) NOT NULL
    sent_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    status    VARCHAR(16) NOT NULL DEFAULT 'sent'
        'sent'   = delivered; the recipient is NEVER re-mailed for
                   this change;
        'failed' = the send raised; retried on the NEXT daily run
                   (each attempt appends a new row — rows are never
                   updated or deleted, ISO 27001).

The partial unique index (change_id, tenant_id, lower(email)) WHERE
status='sent' enforces in the database "one sent row per recipient per
change" even if two scheduler processes race.

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. Dry run (default) PRINTS
the SQL and exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zf_fx85_notice_deliveries.py           # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zf_fx85_notice_deliveries.py --commit  # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order) — this file sorts after
20261007ze (FX.85 p1b notice subscriptions). models.py is bind-mounted
live into the API container, so the table must exist before any FX.85
p2a code path writes to it. NOT applied by this task (model/migration/
service/scheduler/tests only) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "subprocessor_notice_deliveries table",
        """
CREATE TABLE IF NOT EXISTS subprocessor_notice_deliveries (
    id BIGSERIAL PRIMARY KEY,
    change_id BIGINT NOT NULL REFERENCES subprocessor_changes(id),
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    email VARCHAR(255) NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status VARCHAR(16) NOT NULL DEFAULT 'sent'
);
""",
    ),
    (
        "subprocessor_notice_deliveries indexes",
        """
CREATE INDEX IF NOT EXISTS ix_subprocessor_notice_deliveries_change_id
    ON subprocessor_notice_deliveries (change_id);
CREATE INDEX IF NOT EXISTS ix_subprocessor_notice_deliveries_tenant_id
    ON subprocessor_notice_deliveries (tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS
    uq_subprocessor_notice_deliveries_sent
    ON subprocessor_notice_deliveries (change_id, tenant_id, lower(email))
    WHERE status = 'sent';
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
    print("FX.85 p2a notice delivery schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
