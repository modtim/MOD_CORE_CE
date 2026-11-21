#!/usr/bin/env python3
"""sz-fortress-p256.2 (2.2b: HSM key rotation + platform release labeling)
— the signing-key rotation AUDIT TRAIL table.

revision      : 20261010h_fortress_22_hsm_key_metadata
down_revision : 20261010g_production_output_fingerprints

`services/run_proof_bundle.py:sign_bundle()` signs a key once and uses it
forever today; there is no record of WHEN a key was rotated, what it
replaced, or who/why. This migration only creates the storage for that
audit trail (`signing_key_rotations`); `run_proof_bundle.py` gains the
read/write helpers (`current_rotation_epoch`, `record_rotation`) in the
same change, but no code path actually generates/swaps a new signing key
yet — that is a later rotation-trigger task.

Columns, per the sz-fortress-p256.2 decomp row:
    id                   BIGSERIAL PRIMARY KEY
    key_id               TEXT NOT NULL — the (new) key this rotation
                         activates.
    previous_key_id      TEXT NULL — the key it replaces, or NULL for a
                         lineage's first recorded rotation.
    alg                  TEXT NOT NULL — signing algorithm ("ed25519",
                         "pkcs11", ...).
    rotation_epoch       INTEGER NOT NULL — monotonically increasing
                         generation counter for this key lineage.
    rotated_by_user_id   BIGINT NULL REFERENCES users (id)
                         ON DELETE SET NULL — nullable + SET NULL so a
                         later-deleted user's historical rotation events
                         stay (no-hard-delete ISO posture).
    reason               TEXT NULL — free-text operator note.
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()

Index: ix_signing_key_rotations_key_id on (key_id).

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. No models.py ORM
class is added by this migration (models.py is reserved for another
in-flight task this round) — `run_proof_bundle.py` queries this table via
plain `db.execute(text(...))`, the same pattern
`services/relay_metering.py` uses for its own plain-SQL tables.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261010h_fortress_22_hsm_key_metadata.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "signing_key_rotations"

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-fortress-p256.2 — signing-key rotation audit "
        "trail)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    key_id TEXT NOT NULL,
    previous_key_id TEXT NULL,
    alg TEXT NOT NULL,
    rotation_epoch INTEGER NOT NULL,
    rotated_by_user_id BIGINT NULL REFERENCES users (id)
        ON DELETE SET NULL,
    reason TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    ),
    (
        f"{_TABLE} index (key_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_key_id
    ON {_TABLE} (key_id);
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
                   help="Apply the CREATE TABLE + index steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (rotation audit rows are kept "
              "permanently — no-hard-delete ISO posture)")
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
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "sz-fortress-p256.2 ready: signing_key_rotations exists; "
        "run_proof_bundle.py can record+read key-rotation epochs."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
