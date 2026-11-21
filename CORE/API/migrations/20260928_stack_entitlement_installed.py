"""GF.12 — catalog stack install: remember the materialized stack per
tenant entitlement.

Adds ONE nullable column:

  tenant_stack_entitlements.installed_stack_id
      — FK to persistent_stacks.id, ON DELETE SET NULL. Set by
        services/stack_install_service when the tenant's copy of a purchased
        stack listing is created; makes POST /v1/catalog/stack/{id}/install
        idempotent and powers the library's `installed_stack_id` /
        `update_available` fields.

Purely additive — no existing data touched (the column is NULL until an
install happens). Idempotent: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT
EXISTS; safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260928_stack_entitlement_installed.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ALTER_TENANT_STACK_ENTITLEMENTS_SQL = """
ALTER TABLE tenant_stack_entitlements
    ADD COLUMN IF NOT EXISTS installed_stack_id
    INTEGER REFERENCES persistent_stacks(id) ON DELETE SET NULL;
"""

INDEX_TENANT_STACK_ENTITLEMENTS_INSTALLED_SQL = """
CREATE INDEX IF NOT EXISTS ix_tenant_stack_entitlements_installed_stack_id
    ON tenant_stack_entitlements (installed_stack_id);
"""

STATEMENTS = [
    ALTER_TENANT_STACK_ENTITLEMENTS_SQL,
    INDEX_TENANT_STACK_ENTITLEMENTS_INSTALLED_SQL,
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
        for s in STATEMENTS:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    # Create the new objects owned by modTech (not the transient Vault dynamic
    # role that runs this migration) so ownership matches every other object
    # and the API's startup ALTER-OWNER reconcile stays a no-op. The app's
    # dynamic role is a member of modTech, so SET ROLE succeeds; fall back to
    # plain execution if it can't (dev/sqlite).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for s in STATEMENTS:
        cur.execute(s)
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("tenant_stack_entitlements.installed_stack_id ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
