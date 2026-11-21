#!/usr/bin/env python3
"""fortress-5.9 (S) -- ADD workflow_certifications signature/claim/kind columns.

revision      : 20261013g_fortress_59_certification_signature_columns
down_revision : 20261013f_fortress_45_region_storage

Q-CERTSIG=A (owner 10-12): third-party certifier signatures are verified at
issue time (``services/workflow_certifications.py:third_party_issue`` ->
``verify_third_party_signature``) but never stored. This migration adds the
three columns needed to persist the verified signature for audit, without
changing verification behavior (that wiring is a separate, HOLD-until-this
task: 984b).

Columns (additive):

  - signature_b64 TEXT NULL -- the base64 Ed25519 signature supplied by the
    caller (only ever populated for third-party issues).
  - claim_sha256 TEXT NULL -- sha256 hex digest of the canonical claim bytes
    (services.workflow_certifications.canonical_claim_bytes) the signature
    covered, so the exact signed payload can be reconstructed/audited later
    without re-deriving it from mutable state.
  - certification_kind TEXT NOT NULL DEFAULT 'platform' with
    CHECK (certification_kind IN ('platform', 'third_party')) named
    ck_workflow_certifications_kind. Existing rows (all platform-issued so
    far) get the server default; no backfill script needed.

All three nullable/defaulted -- strictly additive, no backfill of existing
rows beyond the server default on certification_kind.

``id`` on workflow_certifications stays BIGSERIAL PRIMARY KEY from the
original table (20261012v) -- never a max()-subquery DEFAULT (Postgres
rejects subqueries in a DEFAULT clause). This migration adds no FK, so there
is no ON DELETE clause at all; where one is ever written it is
"ON DELETE SET NULL" (two words), never "ONDELETE".

Idempotent: ADD COLUMN IF NOT EXISTS for all three columns; the CHECK
constraint is added via a DO $$ ... IF NOT EXISTS (pg_constraint) ... $$
guard (ALTER TABLE ADD CONSTRAINT has no IF NOT EXISTS form -- pattern:
20261013c_fortress_65_secret_he_key_columns.py / 20261012k). Safe to re-run.

Downgrade (DEV-ONLY path): drops the CHECK constraint and the three columns,
in dependency order. Prod never runs downgrade -- ISO 27001's no-hard-delete
rule governs ROWS, not schema; dropping an additive, still-empty column set
on a dev/test DB is not a data-destruction event the way deleting a row
would be.

DRY-RUN by default; --commit applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261013g_fortress_59_certification_signature_columns.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "workflow_certifications"

_UPGRADE_STATEMENTS = [
    (
        f"{_TABLE}.signature_b64 TEXT NULL",
        f"""
ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS
    signature_b64 TEXT;
""",
    ),
    (
        f"{_TABLE}.claim_sha256 TEXT NULL",
        f"""
ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS
    claim_sha256 TEXT;
""",
    ),
    (
        f"{_TABLE}.certification_kind TEXT NOT NULL DEFAULT 'platform'",
        f"""
ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS
    certification_kind TEXT NOT NULL DEFAULT 'platform';
""",
    ),
    (
        f"{_TABLE} CHECK ck_workflow_certifications_kind",
        f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'ck_workflow_certifications_kind') THEN
        ALTER TABLE {_TABLE}
            ADD CONSTRAINT ck_workflow_certifications_kind
                CHECK (certification_kind IN ('platform', 'third_party'));
    END IF;
END
$$;
""",
    ),
]

_DOWNGRADE_STATEMENTS = [
    (
        "drop constraint ck_workflow_certifications_kind",
        f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "
        f"ck_workflow_certifications_kind;",
    ),
    (
        "drop column certification_kind",
        f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS certification_kind;",
    ),
    (
        "drop column claim_sha256",
        f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS claim_sha256;",
    ),
    (
        "drop column signature_b64",
        f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS signature_b64;",
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
                   help="Apply the ALTER steps. Without this flag, dry-run only.")
    p.add_argument("--downgrade", action="store_true",
                   help="DEV-ONLY: drop the columns/constraint instead of adding them.")
    args = p.parse_args()

    statements = _DOWNGRADE_STATEMENTS if args.downgrade else _UPGRADE_STATEMENTS

    if not args.commit:
        for label, sql in statements:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        if not args.downgrade:
            print("DRY  downgrade: dev-only, drops the constraint and three "
                  "columns added here (prod never runs downgrade -- ISO "
                  "27001's no-hard-delete rule governs rows, not schema)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in statements:
        try:
            cur.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    if args.downgrade:
        print("fortress-5.9 S downgrade applied: workflow_certifications "
              "signature_b64/claim_sha256/certification_kind removed "
              "(dev-only).")
    else:
        print(
            "fortress-5.9 S ready: workflow_certifications carries "
            "signature_b64 (TEXT NULL), claim_sha256 (TEXT NULL), "
            "certification_kind (TEXT NOT NULL DEFAULT 'platform', CHECK "
            "ck_workflow_certifications_kind)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
