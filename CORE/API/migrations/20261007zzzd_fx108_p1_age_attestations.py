#!/usr/bin/env python3
"""GSEC FX.108 p1 (owner decision Q95=A) — the hosted sign-up AGE ATTESTATION
record (models.SignupAgeAttestation) + the two audit-action labels that carry
it.

Q95=A: "hosted-cloud sign-up requires age 16+ (tick-box confirmation),
under-13s never on the hosted cloud". So the hosted self-serve sign-up must
carry an attestation, and the platform must be able to PROVE per account that
it did — timestamped and audited. This migration is that proof's home.

    signup_age_attestations
      id                     BIGSERIAL PRIMARY KEY
      user_id                INTEGER NOT NULL  FK users(id)   the account
      tenant_id              INTEGER NOT NULL  FK tenants(id)  the tenant
      attestation            VARCHAR(16) NOT NULL DEFAULT '16_plus'
                             '16_plus' (the hosted tick) | '13_15' (reserved
                             for the education/on-prem flow, FX.108 p3)
      attested_at            TIMESTAMPTZ NOT NULL   WHEN the user attested
      created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
      deleted_at             TIMESTAMPTZ NULL       soft delete ONLY

NEW TABLE ONLY (models.py is bind-mounted live: no column was added to any
existing table). SOFT-DELETE ONLY (ISO 27001 — the attestation is the
compliance record of "this account was created with an age attestation at
this time"; removal is a deleted_at stamp, and there is NO ON DELETE CASCADE
/ SET NULL on either FK: deleting a user or a tenant must never erase the
record that its account was age-attested at creation).

DATA MINIMISATION (why there is no birth date, no IP, no email here): the
compliance question is "was this account 16+ when it was created", not "when
was this person born". A birth year would be personal data the platform has
no need for, so the row stores only the BAND the user ticked. A refused
underage attempt writes NO row here at all (user_id is NOT NULL: an account
that was never created has no attestation) and its audit metadata carries
only the band — never a name, email or birth year.

Constraints (the DB enforces the compliance rules, not just the service):
    ck_signup_age_attestations_band     the two bands only
    ck_signup_age_attestations_clock    an attestation always says WHEN
                                        (the owner decision's "stored with
                                        timestamp")

Indexes: user_id+attested_at (the per-account proof read), attestation+
attested_at (the education vertical's band count).

The TWO audit-action LABELS (AGE_ATTESTATION_RECORDED / SIGNUP_UNDERAGE_BLOCKED)
ship in the same step: models.AuditAction is Column(Enum(AuditAction)) with no
values_callable, so SQLAlchemy writes the member NAME and the live `auditaction`
PG enum needs those exact UPPERCASE labels or every row is silently dropped
(the FX.158 p2a lowercase-label bug class; tests/test_audit_action_enum_sync.py
guards it). ALTER TYPE ... ADD VALUE cannot run inside a transaction, which is
why the whole script runs on an autocommit connection.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching
the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzd_fx108_p1_age_attestations.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007zzzd_fx108_p1_age_attestations.py --commit   # apply

NOT applied by the p1 task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "signup_age_attestations"


_STATEMENTS = [
    (
        f"{_TABLE} table (FX.108 p1)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    attestation VARCHAR(16) NOT NULL DEFAULT '16_plus',
    attested_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_{_TABLE}_band CHECK (
        attestation IN ('16_plus', '13_15')
    ),
    CONSTRAINT ck_{_TABLE}_clock CHECK (
        attested_at IS NOT NULL
    )
);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_user_attested
    ON {_TABLE} (user_id, attested_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_attestation
    ON {_TABLE} (attestation, attested_at);
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_deleted_at
    ON {_TABLE} (deleted_at);
""",
    ),
    (
        "auditaction labels for the two FX.108 p1 age-attestation actions",
        """
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'AGE_ATTESTATION_RECORDED';
ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'SIGNUP_UNDERAGE_BLOCKED';
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
        "FX.108 p1 ready: signup_age_attestations exists and the two "
        "AGE_ATTESTATION_* / SIGNUP_UNDERAGE_BLOCKED audit labels are on the "
        "live enum."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
