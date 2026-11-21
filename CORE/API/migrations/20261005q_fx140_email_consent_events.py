"""FX.140 part 2 (owner decision Q128=A) — add the `email_consent_events` table.

Append-only email-consent record: one row per consent DECISION (never an
edit), so the current consent for a (user, category) pair is the LATEST
event for that pair. `granted` True = consented, False = opted out;
`source` (signup | preferences | unsubscribe_link | admin) records how the
decision was made; `wording_version` pins the consent wording shown at the
time of the decision (correct-records requirement: the wording that
justified the boolean is recoverable).

Columns and constraints mirror models.EmailConsentEvent exactly:
* id BIGSERIAL — BigInteger PK autoincrement in the model;
* user_id NOT NULL + indexed, tenant_id nullable (a user with no tenant);
* category VARCHAR(48) — free-form lowercase vocabulary (product,
  marketing, security, billing, ...), NOT an enum (the service layer
  decides which categories are service, Q128=A);
* source VARCHAR(32) CHECK — the four decision sources;
* wording_version NOT NULL default 'email-consent-v1';
* created_at tz timestamp default now() — the ordering key.

There is deliberately NO updated_at and NO deleted_at: the table is
append-only (ISO 27001 — nothing is ever updated or deleted).

The repo's migration runner is `tools/mod_migrate.py`, not Alembic:
steps are discovered as `*.py` files in this directory and run in
filename order, each recorded in the append-only `release_steps`
ledger. Naming convention `YYYYMMDD[_letter].slug.py` — this file
sorts after 20261005p.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \
        migrations/20261005q_fx140_email_consent_events.py            # dry run
    docker exec -w /app my_fastapi_app python3 \
        migrations/20261005q_fx140_email_consent_events.py --commit   # apply

Additive only: CREATE TABLE/INDEX IF NOT EXISTS — nothing is dropped
or truncated.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "email_consent_events"

TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    tenant_id INTEGER REFERENCES tenants(id),
    category VARCHAR(48) NOT NULL,
    granted BOOLEAN NOT NULL,
    source VARCHAR(32) NOT NULL DEFAULT 'signup',
    wording_version VARCHAR(32) NOT NULL DEFAULT 'email-consent-v1',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT email_consent_events_source_valid
        CHECK (source IN ('signup', 'preferences',
                          'unsubscribe_link', 'admin'))
);
"""

INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS ix_email_consent_events_user_category
    ON {_TABLE} (user_id, category);
"""

_STATEMENTS = [
    (f"CREATE TABLE {_TABLE}", TABLE_SQL),
    ("CREATE INDEX ix_email_consent_events_user_category", INDEX_SQL),
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
