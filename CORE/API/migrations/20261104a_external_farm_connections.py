#!/usr/bin/env python3
"""BRIEF-EXTFARM-1 — NEW table `external_farm_connections` + 2 audit enum values.

The External Orchestrators page (routers/system_external.py) is read-only today:
GET /system/external only aggregates kubernetes_submit / qube_submit /
deadline_submit rows of api_task_queue. There is no way to REGISTER a farm and
nowhere to keep its credential. This migration lands the data layer: one row
per (tenant, farm) with kind/name/endpoint as plain configuration, and
`vault_path` holding ONLY a Vault KV v2 reference path — the credential itself
is written to Vault by services/external_farms.py and is never a DB column
(SEC-1-001 / ISO 27001 AC-45: no plaintext secret at rest in the DB).

revision      : 20261104a_external_farm_connections
down_revision : 20261029a_worker_license_soft_delete

Idempotent: every CREATE/ALTER step is IF NOT EXISTS, the enum values use
`ADD VALUE IF NOT EXISTS`, so re-running is a no-op. Reversible: `--downgrade
[--commit]` drops the table; the two audit enum values are NOT removed (Postgres
has no ALTER TYPE REMOVE VALUE and a stale enum member is harmless — old rows
keep their meaning). Additive only: no existing column or index is changed.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the lane
-- the lead applies it:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261104a_external_farm_connections.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "external_farm_connections table",
        """
CREATE TABLE IF NOT EXISTS external_farm_connections (
    id BIGSERIAL PRIMARY KEY,
    kind VARCHAR(32) NOT NULL,
    name VARCHAR(120) NOT NULL,
    endpoint VARCHAR(512) NOT NULL,
    vault_path VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE
);
""",
    ),
    (
        "tenant_id index",
        """
CREATE INDEX IF NOT EXISTS ix_external_farm_connections_tenant_id
    ON external_farm_connections (tenant_id);
""",
    ),
    (
        "unique scope index (tenant_id, name)",
        """
CREATE UNIQUE INDEX IF NOT EXISTS uq_external_farm_connections_tenant_name
    ON external_farm_connections (tenant_id, name);
""",
    ),
    (
        "kind check constraint (ALTER TABLE ADD CHECK has no IF NOT EXISTS form)",
        """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'ck_external_farm_connections_kind') THEN
        ALTER TABLE external_farm_connections
            ADD CONSTRAINT ck_external_farm_connections_kind
                CHECK (kind IN ('kubernetes', 'qube', 'deadline'));
    END IF;
END
$$;
""",
    ),
]

# Postgres enum values must be added one at a time OUTSIDE a transaction block
# (ALTER TYPE ADD VALUE); the autocommit connection this script uses handles
# that, exactly like 20260605_add_scim_audit_action_enum_values.py. The VALUE
# NAMES are the UPPERCASE Python member names of models.AuditAction.
_AUDIT_VALUES = [
    "EXTERNAL_FARM_CREATED",
    "EXTERNAL_FARM_DELETED",
]

_DOWNGRADE = """
DROP TABLE IF EXISTS external_farm_connections;
"""


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the DDL. Without this flag, dry-run only.")
    p.add_argument("--downgrade", action="store_true",
                   help="Drop the table (dry-run unless --commit); enum values stay.")
    args = p.parse_args()

    if args.downgrade:
        if not args.commit:
            print("DRY  would: " + _DOWNGRADE)
            print("DRY  audit enum values are NOT removed (see docstring).")
            return 0
        conn = _get_conn()
        cur = conn.cursor()
        for stmt in [s.strip() for s in _DOWNGRADE.split(";") if s.strip()]:
            try:
                cur.execute(stmt + ";")
            except Exception as e:  # noqa: BLE001
                print(f"FAIL downgrade: {e}")
                return 1
        cur.close()
        conn.close()
        print("OK   downgrade: external_farm_connections dropped "
              "(audit enum values kept).")
        return 0

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        for v in _AUDIT_VALUES:
            print(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{v}';")
        print("DRY  downgrade available: --downgrade [--commit]")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        try:
            cur.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
        print(f"OK   {label}")
    for v in _AUDIT_VALUES:
        try:
            cur.execute(f"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '{v}'")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL audit enum {v}: {e}")
            return 1
        print(f"OK   audit enum {v}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
