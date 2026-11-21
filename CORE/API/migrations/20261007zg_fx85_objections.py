"""GSEC FX.85 p2b (owner decision Q70=A) — tenant objection handling for
subprocessor changes.

subprocessor_objections (NEW): one row per tenant objection to one
announced subprocessor change, raisable only while the change's
effective_at is still in the future (the 30-day notice window).
    id                BIGSERIAL PK
    change_id         BIGINT NOT NULL  subprocessor_changes.id FK (indexed)
    tenant_id         INTEGER NOT NULL  tenants.id FK (indexed)
    raised_by_user_id INTEGER NULL   users.id FK
    reason            TEXT NOT NULL  (service-capped at 2000 chars)
    status            VARCHAR(32) NOT NULL DEFAULT 'open'
        CHECK IN ('open', 'acknowledged', 'resolved_accepted',
                  'resolved_withdrawn')
        'open'                  = awaiting a platform response;
        'acknowledged'          = a platform admin acknowledged it;
        'resolved_accepted'     = resolved; the platform stands by the
                                  change;
        'resolved_withdrawn'    = resolved; the platform withdraws /
                                  revises the change.
    platform_response TEXT NULL  (the admin's reply, null until given)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
    updated_at        TIMESTAMPTZ NULL  (NULL on the initial 'open' row;
                      stamped on every transition)
    resolved_at       TIMESTAMPTZ NULL  (stamped on a resolved_*
                      transition; NULL while open/acknowledged)

Lifecycle only — NO UPDATE/DELETE path anywhere (ISO 27001): the row
stays with its transition stamps; 'status' moves forward and is never
cleared or the row removed.

The partial unique index (change_id, tenant_id) WHERE status='open'
enforces in the database "one OPEN objection per tenant per change"
(even if two admins race) — matching the service-level idempotency in
services/subprocessor_changes.py::raise_objection (an existing open
objection is returned as-is).

Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. Dry run (default) PRINTS
the SQL and exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zg_fx85_objections.py           # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zg_fx85_objections.py --commit  # apply

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order) — this file sorts after
20261007zf (FX.85 p2a notice deliveries). models.py is bind-mounted
live into the API container, so the table must exist before any FX.85
p2b code path writes to it. NOT applied by this task (model/migration/
service/router/tests only) — do not run with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "subprocessor_objections table",
        """
CREATE TABLE IF NOT EXISTS subprocessor_objections (
    id BIGSERIAL PRIMARY KEY,
    change_id BIGINT NOT NULL REFERENCES subprocessor_changes(id),
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    raised_by_user_id INTEGER NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'open',
    platform_response TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NULL,
    resolved_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_subprocessor_objections_status CHECK (
        status IN ('open', 'acknowledged', 'resolved_accepted',
                   'resolved_withdrawn')
    )
);
""",
    ),
    (
        "subprocessor_objections indexes",
        """
CREATE INDEX IF NOT EXISTS ix_subprocessor_objections_change_id
    ON subprocessor_objections (change_id);
CREATE INDEX IF NOT EXISTS ix_subprocessor_objections_tenant_id
    ON subprocessor_objections (tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS
    uq_subprocessor_objections_open
    ON subprocessor_objections (change_id, tenant_id)
    WHERE status = 'open';
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
    print("FX.85 p2b objections schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
