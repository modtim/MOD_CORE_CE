#!/usr/bin/env python3
"""CUIP.3 (FEAT.CUI-PHONES) — the ONE new table: enrolled MOBILE DEVICES.

revision      : 20261009a_cui_phones_mobile_devices
down_revision : 20261008i_fx135_p3b2_trial_device_fingerprints

CUI on phones (DECIDED B shelf + DECIDED C severity tiers, cui-phones.md) needs
a record of every phone that enrolled against a tenant: which device, which
platform, which app build, the HASH of its device token, and — when the phone
reported one — the attestation/integrity tier it currently holds.

Structure copied from the newest new-table migrations
(20261008i_fx135_p3b2_trial_device_fingerprints.py, 20261008h_bug_b59a_knowledge_source_binding.py,
20261007zzz_fx115_p3_node_lockdown_exceptions.py): Class A argparse, `--commit`
to apply, dry-run (prints SQL) by default.

    mobile_devices
      id                   BIGSERIAL PRIMARY KEY
      tenant_id            INTEGER NOT NULL   FK tenants(id) (no ON DELETE)
      device_id            VARCHAR(64) UNIQUE  client-generated device id
      platform             VARCHAR(16)         'android' | 'ios'
      app_build            VARCHAR(64)         app build that enrolled
      token_hash           VARCHAR(128)        sha-256 of the device token
      integrity_tier       VARCHAR(16) NULL    attestation verdict (NULL = none yet)
      enrolled_by_user_id  INTEGER NULL        FK users(id) ON DELETE SET NULL
      created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
      last_seen_at         TIMESTAMPTZ NULL
      revoked_at           TIMESTAMPTZ NULL    soft revoke ONLY

`id` is `BIGSERIAL PRIMARY KEY` — never a max()-subquery DEFAULT (Postgres
rejects subqueries in a DEFAULT clause).

`tenant_id` is NOT NULL with NO ON DELETE clause: tenants are never hard-deleted
(ISO soft delete), so the owner link must never be nulled — a tenant delete
trying to erase it would fail the NOT NULL, which is the point.
`enrolled_by_user_id` carries `ON DELETE SET NULL` (two words): losing the
enrolling user must never erase the device record (ISO 27001 A.18.1.3,
protection of records). Revoke is a soft marker (`revoked_at`), never a real
removal: this migration contains no DELETE and no DROP of any kind; downgrade
is a NO-OP.

Idempotent: CREATE TABLE / CREATE ... INDEX IF NOT EXISTS, so a re-run is a no-op.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the
lane — the lead applies it.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "mobile_devices"


_STATEMENTS = [
    (
        f"{_TABLE} table (CUIP.3, enrolled mobile devices)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    device_id VARCHAR(64) NOT NULL UNIQUE,
    platform VARCHAR(16) NOT NULL,
    app_build VARCHAR(64) NOT NULL,
    token_hash VARCHAR(128) NOT NULL,
    integrity_tier VARCHAR(16) NULL,
    enrolled_by_user_id INTEGER NULL REFERENCES users (id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NULL,
    revoked_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_id
    ON {_TABLE} (tenant_id);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_enrolled_by_user_id
    ON {_TABLE} (enrolled_by_user_id);
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
        "CUIP.3 ready: mobile_devices exists with unique device_id and "
        "soft-revoke (revoked_at) only."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
