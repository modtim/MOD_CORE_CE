#!/usr/bin/env python3
"""FX.135 p3b2 (owner decision Q123=A) — the ONE new table for DEVICE trial records.

revision      : 20261008i_fx135_p3b2_trial_device_fingerprints
down_revision : 20261008h_bug_b59a_knowledge_source_binding

Q123=A ("hosted-cloud fraud and compute abuse") says "Throwaway email domains
blocked; one free trial per card or device." FX.135 p2 built the CARD half
(`trial_card_fingerprints`, migration 20261007zzzg_fx135_p2_trial_card_fingerprints.py).
This is the DEVICE half of the SAME rule: the record of which device already
claimed a free trial.

There is no vendor token for a browser/device (the trial-start routes carry
only plan + days, and the login path records just the caller's client IP and
user-agent), so the value stored here is a HASH the service derives from the
two signals the request already carries — the client IP collapsed to its /24
network and the user-agent string. The RAW signals are never stored: no full
IP, no user-agent verbatim, nothing device-specific beyond that coarse pair.

    trial_device_fingerprints
      id                  BIGSERIAL PRIMARY KEY
      device_fingerprint  VARCHAR(64) NOT NULL   sha-256(ip/24 + user-agent)
      tenant_id           INTEGER NOT NULL       FK tenants(id)  owner tenant
      created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
      deleted_at          TIMESTAMPTZ NULL       soft delete ONLY

`id` is `BIGSERIAL PRIMARY KEY` — NOT `DEFAULT COALESCE((SELECT max(id) ...))`,
which Postgres rejects (a subquery is not a legal DEFAULT).

Constraints (the DB enforces the rule, not just the service):
    uq_trial_device_fp_live   one LIVE row per device_fingerprint — the WHERE
                              clause excludes tombstones, so releasing a device
                              (soft-delete) and re-issuing it never collides
                              with its own soft-deleted row.

FKs carry NO `ON DELETE CASCADE` / `ON DELETE SET NULL` clause: deleting a
tenant must never erase the record of the trial its device once claimed (ISO
27001 A.18.1.3 — protection of records).

Index names use the `fp` short form: `trial_device_fingerprints` is one byte
longer than its card sibling, and the card half's names already sit exactly at
Postgres' 31-byte identifier limit.

NEW TABLE ONLY (models.py is bind-mounted live and shared by every lane: no
column was added to any existing table). Idempotent: CREATE ... IF NOT EXISTS,
so a re-run is a no-op. Downgrade is a NO-OP (ISO 27001 — never drop the
records).

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the
lane — the lead applies it.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "trial_device_fingerprints"
_SHORT = "fp"             # Postgres truncates identifiers at 31 bytes


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.135 p3b2, device half of Q123=A)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    device_fingerprint VARCHAR(64) NOT NULL,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_trial_{_SHORT}_live
    ON {_TABLE} (device_fingerprint) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_trial_{_SHORT}_device
    ON {_TABLE} (device_fingerprint);
CREATE INDEX IF NOT EXISTS ix_trial_{_SHORT}_tenant
    ON {_TABLE} (tenant_id);
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
        "FX.135 p3b2 ready: trial_device_fingerprints exists with one LIVE "
        "row per device (uq_trial_fp_live)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
