#!/usr/bin/env python3
"""FX.145 p2a (GSEC) — the ONE new table: ADMIN/STAFF DEVICE RECORDS.

revision      : 20261009b_fx145_p2a_admin_device_records
down_revision : 20261009a_cui_phones_mobile_devices

FX.145 (owner decision Q132=A) requires every admin/staff device that reaches
an admin console to be recorded in the FX.126 asset inventory (office-device
kind), carry the device security policy, and get a yearly owner-confirmed
self-check. FX.126's inventory is COMPUTED from the compute fleet
(worker_rows/manager_rows/stack_rows over WorkerLicense/Manager/StackDeploy)
— it has no type/kind column to hold an office device, and models.py is bind-
mounted live and shared by every lane, so no column was added to any existing
table. This is the ONE new table that carries the office-device kind;
services/admin_device_records.py feeds it into asset_inventory.build_inventory
as the `devices` rows (kind='device'), the shape FX.145 p2b's yearly self-
check sweep (services/device_self_check.py `_build_devices`) already flattens.

Structure copied from the newest new-table migrations
(20261009a_cui_phones_mobile_devices.py, 20261008i_fx135_p3b2_trial_device_fingerprints.py):
Class A argparse, `--commit` to apply, dry-run (prints SQL) by default.

    admin_devices
      id                 BIGSERIAL PRIMARY KEY
      tenant_id          INTEGER NULL      FK tenants(id) (no ON DELETE)
      owner_user_id      INTEGER NULL      FK users(id) ON DELETE SET NULL
      label              VARCHAR(128) NOT NULL
      device_type        VARCHAR(16) NOT NULL  'workstation'|'phone'|'tablet'
      os                 VARCHAR(64) NULL
      serial_or_hw_id    VARCHAR(128) NULL
      tailnet_node_id    VARCHAR(64) NULL  mesh-enrolled node id
      enrolled_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
      last_self_check_at TIMESTAMPTZ NULL
      status             VARCHAR(24) NOT NULL DEFAULT 'active'
      archived_at        TIMESTAMPTZ NULL  soft archive ONLY
      created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
      updated_at         TIMESTAMPTZ NULL

`id` is `BIGSERIAL PRIMARY KEY` — never a max()-subquery DEFAULT (Postgres
rejects subqueries in a DEFAULT clause).

`tenant_id` is NULLABLE (NULL = platform-scope: the platform admins' own
workstations/phones) with NO ON DELETE clause: tenants are never hard-deleted
(ISO soft delete), so the owner link must never be nulled.
`owner_user_id` carries `ON DELETE SET NULL` (two words): losing the owning
user must never erase the device record — the row is what the yearly self-
check and the FX.145 p3a mesh/ingress gate read.

Archive is a soft marker (`archived_at` + the SoftDeleteMixin `deleted_at`
tombstone), never a removal: this migration contains no DELETE and no DROP
of any kind; downgrade is a NO-OP (ISO 27001 A.18.1.3, protection of records).

Idempotent: CREATE TABLE / CREATE ... INDEX IF NOT EXISTS, so a re-run is a
no-op.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the
lane — the lead applies it.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "admin_devices"


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.145 p2a, admin/staff device records)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NULL REFERENCES tenants (id),
    owner_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    label VARCHAR(128) NOT NULL,
    device_type VARCHAR(16) NOT NULL,
    os VARCHAR(64) NULL,
    serial_or_hw_id VARCHAR(128) NULL,
    tailnet_node_id VARCHAR(64) NULL,
    enrolled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_self_check_at TIMESTAMPTZ NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'active',
    archived_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL,
    CHECK (device_type IN ('workstation', 'phone', 'tablet')),
    CHECK (status IN ('active', 'inactive', 'retired')),
    CHECK (archived_at IS NULL OR status = 'retired')
);
""",
    ),
    (
        f"{_TABLE} indexes",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_id
    ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_owner_user_id
    ON {_TABLE} (owner_user_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tailnet_node_id
    ON {_TABLE} (tailnet_node_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_last_self_check_at
    ON {_TABLE} (last_self_check_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_archived_at
    ON {_TABLE} (archived_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_created_at
    ON {_TABLE} (created_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_status
    ON {_TABLE} (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_owner
    ON {_TABLE} (owner_user_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at
    ON {_TABLE} (deleted_at);
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
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the CREATE + index steps. Without this flag, "
                        "dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (ISO 27001 — never drop the records)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        for stmt in (s.strip() for s in sql.split(";") if s.strip()):
            try:
                cur.execute(stmt)
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {label}: {e}")
                return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "FX.145 p2a ready: admin_devices exists with soft archive "
        "(archived_at/deleted_at) only."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
