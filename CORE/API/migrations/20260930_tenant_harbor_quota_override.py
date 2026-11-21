"""GG.HQ.2 — tenants.harbor_storage_quota_override_bytes + audit enum value.

Per-tenant Harbor storage quota override. Today a tenant's Harbor quota
comes only from its plan (`utils/harbor_quota_sync.py` converts
`Plan.max_storage_gb` via `_gb_to_quota_bytes`), and the daily reconcile
(`services/harbor_quota_reconcile.py`, 02:00 UTC) re-syncs every tenant —
so any manual Harbor-side quota edit is reverted nightly. This column
lets a platform admin pin a quota in MOD itself:

    NULL  = derive the quota from the plan (today's behaviour)
    -1    = unlimited (Harbor's own sentinel)
    >0    = exact hard cap in bytes

`sync_tenant_harbor_quota_from_plan` prefers a non-NULL override over the
plan value, so the nightly reconcile + every plan-change sync site
respects the override. The setter is the platform-admin endpoint
`PUT /v1/tenants/{tenant_id}/harbor-storage-quota-override`, which also
writes an `ADMIN_HARBOR_STORAGE_QUOTA_OVERRIDE` audit row.

Adds the nullable column (purely additive — no existing data touched,
every existing row gets NULL = plan-derived) plus the new
`admin_harbor_storage_quota_override` value on the `auditaction` enum.

Idempotent: `ADD COLUMN IF NOT EXISTS` + `ALTER TYPE ... ADD VALUE IF NOT
EXISTS`; safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260930_tenant_harbor_quota_override.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys


_TABLE = "tenants"

ADD_OVERRIDE_COLUMN_SQL = """
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS harbor_storage_quota_override_bytes BIGINT;
"""

ADD_AUDIT_VALUE_SQL = """
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'admin_harbor_storage_quota_override'
"""

STATEMENTS = [
    ("add column tenants.harbor_storage_quota_override_bytes",
     ADD_OVERRIDE_COLUMN_SQL),
    ("add auditaction value admin_harbor_storage_quota_override",
     ADD_AUDIT_VALUE_SQL),
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
        for label, sql in STATEMENTS:
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        try:
            cur.execute(f'ALTER TABLE {_TABLE} OWNER TO {owner}')
        except Exception as e:  # noqa: BLE001
            print(f"(ALTER TABLE {_TABLE} OWNER TO {owner} failed: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("tenants.harbor_storage_quota_override_bytes ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
