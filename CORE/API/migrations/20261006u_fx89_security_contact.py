"""FX.89 (owner decision Q75=A) — tenant security contact.

Adds the tenant security-contact fields (primary email, phone, optional
secondary email, last-confirmation stamp) plus the audit-action enum
value that backs `TENANT_SECURITY_CONTACT_UPDATED`:

tenants:
    security_contact_email          VARCHAR(255) NULL  primary contact email
    security_contact_phone          VARCHAR(64)  NULL  phone (E.164-ish)
    security_contact_secondary_email VARCHAR(255) NULL  optional secondary
    security_contact_confirmed_at   TIMESTAMPTZ  NULL  last (re)confirmation

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    TENANT_SECURITY_CONTACT_UPDATED

All tenant columns are nullable — every pre-existing tenant keeps NULL
and security notifications fall back to all tenant admins
(services/tenant_security_contact_service.resolve_security_recipients).
No backfill.

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction
block, so the whole script runs on an autocommit connection (same
pattern as 20261005u_fx93_abuse_audit_actions.py /
20261006t_fx90_malware_scan_audit_actions.py). Idempotent:
ADD COLUMN IF NOT EXISTS / ADD VALUE IF NOT EXISTS.

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order, recorded in the append-only
release_steps ledger) — this file sorts after 20261006t.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261006u_fx89_security_contact.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261006u_fx89_security_contact.py --commit   # apply

models.py is bind-mounted live into the API container, so the columns
must exist before any FX.89 code path touches them — apply immediately
after adding them to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "tenants.security_contact_email column",
        """
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS security_contact_email VARCHAR(255);
""",
    ),
    (
        "tenants.security_contact_phone column",
        """
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS security_contact_phone VARCHAR(64);
""",
    ),
    (
        "tenants.security_contact_secondary_email column",
        """
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS security_contact_secondary_email VARCHAR(255);
""",
    ),
    (
        "tenants.security_contact_confirmed_at column",
        """
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS security_contact_confirmed_at TIMESTAMPTZ;
""",
    ),
    (
        "auditaction.TENANT_SECURITY_CONTACT_UPDATED value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'TENANT_SECURITY_CONTACT_UPDATED';
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
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.89 tenant security contact ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
