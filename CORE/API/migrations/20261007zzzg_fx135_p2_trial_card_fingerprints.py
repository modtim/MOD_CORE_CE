#!/usr/bin/env python3
"""GSEC FX.135 p2 (owner decision Q123=A) — ONE FREE TRIAL PER CARD RECORD.

Q123=A ("hosted-cloud fraud and compute abuse") says "Throwaway email
domains blocked; one free trial per card or device." The card half of that
rule needs a RECORD of which card already claimed a free trial, because
Stripe hands us a stable per-card token — `payment_method.card.fingerprint`
is the SAME value for the same physical card across every customer — so a
second tenant signing up with a card another tenant already used is
refused. This table is that record.

NEW TABLE ONLY (models.py is bind-mounted live and shared by every lane:
NO column was added to any existing table). SOFT-DELETE ONLY (ISO 27001 —
releasing a card soft-deletes the live row and stamps `deleted_at`; nothing
is ever hard-deleted, so the compliance record "this card once owned a
trial" survives). The tenant_id FK carries NO `ON DELETE CASCADE` /
`ON DELETE SET NULL` clause: deleting a tenant must never erase the record
of the trial its card once claimed.

    trial_card_fingerprints
      id                BIGSERIAL PRIMARY KEY
      card_fingerprint  VARCHAR(64) NOT NULL   Stripe card.fingerprint
      tenant_id         INTEGER NOT NULL       FK tenants(id)  owner tenant
      created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
      deleted_at        TIMESTAMPTZ NULL       soft delete ONLY

Constraints (the DB enforces the rule, not just the service):
    uq_trial_card_fingerprints_live   one LIVE row per card_fingerprint —
                              the WHERE clause excludes tombstones, so
                              releasing a card (soft-delete) and re-issuing
                              it never collides with its own soft-deleted
                              row.

Indexes: card_fingerprint (the enforcement read on every trial create),
tenant_id (the "release this tenant's card" lookup + abuse reporting).

DATA MINIMISATION: a fingerprint is an opaque Stripe token, not card data —
no PAN, no last4, no brand, no customer name lives here.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzg_fx135_p2_trial_card_fingerprints.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzg_fx135_p2_trial_card_fingerprints.py --commit   # apply

NOT applied by the p2 task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "trial_card_fingerprints"


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.135 p2)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    card_fingerprint VARCHAR(64) NOT NULL,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live
    ON {_TABLE} (card_fingerprint) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_card
    ON {_TABLE} (card_fingerprint);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant
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
        "FX.135 p2 ready: trial_card_fingerprints exists with one LIVE row "
        "per card (uq_trial_card_fingerprints_live)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
