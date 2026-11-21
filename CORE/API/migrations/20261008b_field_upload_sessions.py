#!/usr/bin/env python3
"""19.3.s2 — field_upload_sessions: resumable field-capture full uploads.

NEW TABLE ONLY (models.py is bind-mounted live and shared by every lane: no
column was added to any existing table). The field client pushes its first
slice in seconds (19.3.s1) and the FULL capture data follows over the same
poor link, which drops requests at random. The resume state cannot live in
memory (the API restarts, the device reboots) nor only in the bucket (the
client must not list S3 to find its own session), so it lives here.

    field_upload_sessions (NEW)
      id                     BIGSERIAL PRIMARY KEY
      tenant_id              INTEGER NOT NULL      FK tenants(id)
      device_id              VARCHAR(64) NOT NULL  the token's device (public
                                                   id, same string the first-
                                                   slice record keys on)
      upload_id              VARCHAR(64) NOT NULL  session handle (client-
                                                   supplied or server-issued)
      capture_id             VARCHAR(128) NULL     the s1 half's key
      state                  VARCHAR(16) NOT NULL  initiated|completed|expired
      total_size             BIGINT NOT NULL       declared bytes
      chunk_size             INTEGER NOT NULL      bytes per chunk (from init)
      expected_sha256        VARCHAR(64) NULL      digest declared at init
      sha256                 VARCHAR(64) NULL      verified at complete
      object_key             TEXT NULL             final assembled object
      total_received_bytes   BIGINT NOT NULL
      parts                  JSONB NOT NULL {}     {"<idx>":{sha256,size,object_key}}
      created_at             TIMESTAMPTZ NOT NULL
      completed_at           TIMESTAMPTZ NULL

NO DROP / DELETE / TRUNCATE anywhere: nothing pre-existing is touched, and the
row is a state machine, never a tombstone — an expired session keeps its row
(ISO 27001 A.18.1.3, records shall not be destroyed).

Idempotent: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS. Dry run
(default, no --commit) PRINTS the SQL and exits 0 WITHOUT touching the
database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261008b_field_upload_sessions.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261008b_field_upload_sessions.py --commit   # apply

NOT applied by the 19.3.s2 task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "field_upload_sessions"


_CREATE = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    device_id VARCHAR(64) NOT NULL,
    upload_id VARCHAR(64) NOT NULL,
    capture_id VARCHAR(128) NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'initiated',
    total_size BIGINT NOT NULL,
    chunk_size INTEGER NOT NULL,
    expected_sha256 VARCHAR(64) NULL,
    sha256 VARCHAR(64) NULL,
    object_key TEXT NULL,
    total_received_bytes BIGINT NOT NULL DEFAULT 0,
    parts JSONB NOT NULL DEFAULT '{{}}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL
);
"""

_STATEMENTS = [
    (f"{_TABLE} table (19.3.s2 resumable field uploads)", _CREATE),
    (f"ix_{_TABLE}_scope index (tenant, device)", f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_scope
    ON {_TABLE} (tenant_id, device_id);
"""),
    (f"ix_{_TABLE}_upload index (upload_id)", f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_upload
    ON {_TABLE} (upload_id);
"""),
    (f"ix_{_TABLE}_created index (created_at)", f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_created
    ON {_TABLE} (created_at);
"""),
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
    print("19.3.s2 ready: field_upload_sessions exists; resumable field "
          "uploads are backed by DB state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
