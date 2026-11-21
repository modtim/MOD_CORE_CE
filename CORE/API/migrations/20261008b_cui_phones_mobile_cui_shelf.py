#!/usr/bin/env python3
"""CUIP.19 (FEAT.CUI-PHONES) — the NEW table: CUI PHONE SHelf (secured staged assets).

revision      : 20261008b_cui_phones_mobile_cui_shelf
down_revision : 20261009a_cui_phones_mobile_devices

CUI on phones (DECIDED B shelf + DECIDED C severity tiers, cui-phones.md) needs
a "shelf" to record assets that a user has marked as staged for transfer to one
enrolled phone. This is a secure staging area — soft revoke only (ISO 27001).

Structure copied from the newest new-table migrations
(20261009a_cui_phones_mobile_devices.py, 20261008i_fx135_p3b2_trial_device_fingerprints.py,
20261008h_bug_b59a_knowledge_source_binding.py, 20261007zzz_fx115_p3_node_lockdown_exceptions.py):
Class A argparse, `--commit` to apply, dry-run (prints SQL) by default.

    mobile_cui_shelf
      id                   BIGSERIAL PRIMARY KEY
      tenant_id            INTEGER NOT NULL   FK tenants(id) (no ON DELETE)
      device_id            BIGINT NULL        FK mobile_devices(id) ON DELETE SET NULL
      resource_type        VARCHAR(64) NOT NULL
      resource_id          VARCHAR(128) NOT NULL
      staged_by_user_id    INTEGER NULL       FK users(id) ON DELETE SET NULL
      staged_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
      expires_at           TIMESTAMPTZ NULL
      revoked_at           TIMESTAMPTZ NULL   soft revoke ONLY
      size_bytes           BIGINT NULL

`id` is `BIGSERIAL PRIMARY KEY` — never a max()-subquery DEFAULT (Postgres
rejects subqueries in a DEFAULT clause).

`tenant_id` is NOT NULL with NO ON DELETE clause: tenants are never hard-deleted
(ISO soft delete), so the owner link must never be nulled — a tenant delete
trying to erase it would fail the NOT NULL, which is the point.
`device_id` carries `ON DELETE SET NULL` (two words): losing the phone must never
erase the shelf record. Same for `staged_by_user_id`.
Revoke is a soft marker (`revoked_at`), never a real removal: this migration
contains no DELETE and no DROP of any kind; downgrade is a NO-OP.

Idempotent: CREATE TABLE / CREATE ... INDEX IF NOT EXISTS, so a re-run is a no-op.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the
lane — the lead applies it.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "mobile_cui_shelf"


_STATEMENTS = [
    (
        f"{_TABLE} table (CUIP.19, CUI phone shelf — staged assets)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    device_id BIGINT NULL REFERENCES mobile_devices (id) ON DELETE SET NULL,
    resource_type VARCHAR(64) NOT NULL,
    resource_id VARCHAR(128) NOT NULL,
    staged_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    staged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NULL,
    revoked_at TIMESTAMPTZ NULL,
    size_bytes BIGINT NULL
);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_id
    ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_device_id
    ON {_TABLE} (device_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_staged_by_user_id
    ON {_TABLE} (staged_by_user_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_revoked_at
    ON {_TABLE} (revoked_at);
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
        "CUIP.19 ready: mobile_cui_shelf exists with soft-revoke (revoked_at) only."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
