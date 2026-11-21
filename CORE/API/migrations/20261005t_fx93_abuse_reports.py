"""FX.93 part 1 (owner decision Q79=A) — add the `abuse_reports` table.

Abuse reporting backend: one row per abuse report submitted by an
authenticated user about a workflow / node / file / output / account /
other target, triaged by platform admins. The table holds the SLA
clock (triage_due_at = created + 1 business day, computed in the
service), the lifecycle status and the resolution note; the audit trail
lives in `permission_audit_logs` (ABUSE_REPORT_CREATED /
ABUSE_REPORT_TRIAGED — see services/abuse_reports.py), this table holds
none.

Columns and constraints mirror models.AbuseReport exactly:
* target_type CHECK — the six target kinds the service validates;
* category CHECK — the seven report categories the service validates;
* status CHECK — the open/triaged/actioned/dismissed/appealed
  lifecycle the service enforces;
* SOFT-DELETE ONLY (ISO 27001 — no hard deletes): `deleted_at` is a
  plain nullable timestamp; removal sets it, the row stays.

The repo's migration runner is `tools/mod_migrate.py`, not Alembic:
steps are discovered as `*.py` files in this directory and run in
filename order, each recorded in the append-only `release_steps`
ledger. Naming convention `YYYYMMDD[_letter].slug.py` — this file
sorts after 20261005s.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261005t_fx93_abuse_reports.py            # dry run
    docker exec my_fastapi_app python3 \\
        migrations/20261005t_fx93_abuse_reports.py --commit   # apply

Additive only: CREATE TABLE/INDEX IF NOT EXISTS — nothing is dropped
or truncated.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "abuse_reports"

TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants(id),
    reporter_user_id INTEGER REFERENCES users(id),
    reporter_email VARCHAR(255),
    target_type VARCHAR(24) NOT NULL,
    target_id VARCHAR(255) NOT NULL,
    category VARCHAR(24) NOT NULL,
    description TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    triage_due_at TIMESTAMP WITH TIME ZONE NOT NULL,
    resolution_note TEXT,
    resolved_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    deleted_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT abuse_reports_target_type_valid
        CHECK (target_type IN ('workflow', 'node', 'file', 'output',
                               'account', 'other')),
    CONSTRAINT abuse_reports_category_valid
        CHECK (category IN ('csam', 'violent', 'harassment', 'ip',
                            'spam', 'malware', 'other')),
    CONSTRAINT abuse_reports_status_valid
        CHECK (status IN ('open', 'triaged', 'actioned', 'dismissed',
                          'appealed'))
);
"""

INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS ix_abuse_reports_tenant_id
    ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_abuse_reports_reporter_user_id
    ON {_TABLE} (reporter_user_id);
CREATE INDEX IF NOT EXISTS ix_abuse_reports_target_type
    ON {_TABLE} (target_type);
CREATE INDEX IF NOT EXISTS ix_abuse_reports_category
    ON {_TABLE} (category);
CREATE INDEX IF NOT EXISTS ix_abuse_reports_status
    ON {_TABLE} (status);
CREATE INDEX IF NOT EXISTS ix_abuse_reports_triage_due_at
    ON {_TABLE} (triage_due_at);
CREATE INDEX IF NOT EXISTS ix_abuse_reports_created_at
    ON {_TABLE} (created_at);
CREATE INDEX IF NOT EXISTS ix_abuse_reports_deleted_at
    ON {_TABLE} (deleted_at);
CREATE INDEX IF NOT EXISTS ix_abuse_reports_status_due
    ON {_TABLE} (status, triage_due_at);
"""

_STATEMENTS = [
    (f"{_TABLE} table", TABLE_SQL),
    (f"indexes on {_TABLE}", INDEX_SQL),
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
