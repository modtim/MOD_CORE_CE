"""GSEC FX.49 part 1 (owner decision Q32=A) — break-glass custody.

The break-glass credential is SPLIT (one half sealed in Vault behind
two-person approval, the other half printed in a sealed envelope in the
owner's physical safe) and each USE needs a second platform admin's
approval that lapses after a window. Part 1 lands the custody model:

break_glass_credentials (NEW):
    name                   VARCHAR(255) NOT NULL UNIQUE   stable display name
    total_shares           INTEGER      NOT NULL          >= 2 (split custody)
    generation             INTEGER      NOT NULL DEFAULT 1  bumped on rotation
    shares                 JSONB        NOT NULL          share_index -> {kind,
                                             sha256} (printed half, hash-only)
                                             or {kind} (Vault half, wrapped).
                                             NEVER the plaintext secret.
    vault_secret_path      VARCHAR(512) NULL               Vault path the sealed
                                             half lives under (identifier only —
                                             the secret itself is in Vault).
    rotation_required      BOOLEAN      NOT NULL DEFAULT false
    rotation_required_at   TIMESTAMPTZ  NULL
    rekeyed_at             TIMESTAMPTZ  NULL
    rekeyed_by_user_id     INTEGER NULL REFERENCES users(id)
    created_at / updated_at TIMESTAMPTZ
    deleted_at             TIMESTAMPTZ  NULL               soft-delete only
                                             (ISO 27001 — no hard deletes)

break_glass_use_requests (NEW):
    credential_id          INTEGER NOT NULL REFERENCES break_glass_credentials(id)
    requested_by_user_id   INTEGER NOT NULL REFERENCES users(id)
    reason                 TEXT    NOT NULL
    status                 VARCHAR(16) NOT NULL  'pending'|'approved'|'denied'|'expired'|'used'
    approved_by_user_id    INTEGER NULL REFERENCES users(id)  MUST != requester
    approved_at            TIMESTAMPTZ NULL
    expires_at             TIMESTAMPTZ NULL       approval window deadline
    used_at                TIMESTAMPTZ NULL       single-use stamp
    used_by_user_id        INTEGER NULL REFERENCES users(id)
    created_at             TIMESTAMPTZ NOT NULL
    deleted_at             TIMESTAMPTZ NULL       soft-delete only
                                             (ISO 27001 — no hard deletes)

auditaction (native PG enum backing models.AuditAction, stores NAMES):
    BREAK_GLASS_REQUESTED
    BREAK_GLASS_APPROVED
    BREAK_GLASS_DENIED
    BREAK_GLASS_EXPIRED
    BREAK_GLASS_USED

`ALTER TYPE auditaction ADD VALUE` cannot run inside a transaction
block, so the whole script runs on an autocommit connection (same
pattern as 20261007e_fx156_alert_ack_action.py). Idempotent:
CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS / ADD VALUE
IF NOT EXISTS.

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order, recorded in the append-only
release_steps ledger) — this file sorts after 20261007g.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007j_fx49_break_glass.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007j_fx49_break_glass.py --commit   # apply

models.py is bind-mounted live into the API container, so the tables +
enum values must exist before any services/break_glass_custody.py code
path touches them — apply immediately after adding the models.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "break_glass_credentials table",
        """
CREATE TABLE IF NOT EXISTS break_glass_credentials (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    total_shares INTEGER NOT NULL DEFAULT 2,
    generation INTEGER NOT NULL DEFAULT 1,
    shares JSONB NOT NULL DEFAULT '{}'::jsonb,
    vault_secret_path VARCHAR(512) NULL,
    rotation_required BOOLEAN NOT NULL DEFAULT false,
    rotation_required_at TIMESTAMPTZ NULL,
    rekeyed_at TIMESTAMPTZ NULL,
    rekeyed_by_user_id INTEGER REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NULL,
    deleted_at TIMESTAMPTZ NULL
);
""",
    ),
    (
        "break_glass_credentials indexes",
        """
CREATE INDEX IF NOT EXISTS ix_break_glass_credentials_generation
    ON break_glass_credentials (generation);
CREATE INDEX IF NOT EXISTS ix_break_glass_credentials_rotation_required
    ON break_glass_credentials (rotation_required);
CREATE INDEX IF NOT EXISTS ix_break_glass_credentials_deleted_at
    ON break_glass_credentials (deleted_at);
CREATE INDEX IF NOT EXISTS ix_break_glass_credentials_created_at
    ON break_glass_credentials (created_at);
""",
    ),
    (
        "break_glass_use_requests table",
        """
CREATE TABLE IF NOT EXISTS break_glass_use_requests (
    id BIGSERIAL PRIMARY KEY,
    credential_id INTEGER NOT NULL REFERENCES break_glass_credentials (id),
    requested_by_user_id INTEGER NOT NULL REFERENCES users (id),
    reason TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    approved_by_user_id INTEGER REFERENCES users (id),
    approved_at TIMESTAMPTZ NULL,
    expires_at TIMESTAMPTZ NULL,
    used_at TIMESTAMPTZ NULL,
    used_by_user_id INTEGER REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL
);
""",
    ),
    (
        "break_glass_use_requests indexes",
        """
CREATE INDEX IF NOT EXISTS ix_break_glass_use_requests_credential_id
    ON break_glass_use_requests (credential_id);
CREATE INDEX IF NOT EXISTS ix_break_glass_use_requests_requested_by_user_id
    ON break_glass_use_requests (requested_by_user_id);
CREATE INDEX IF NOT EXISTS ix_break_glass_use_requests_status
    ON break_glass_use_requests (status);
CREATE INDEX IF NOT EXISTS ix_break_glass_use_requests_approved_at
    ON break_glass_use_requests (approved_at);
CREATE INDEX IF NOT EXISTS ix_break_glass_use_requests_expires_at
    ON break_glass_use_requests (expires_at);
CREATE INDEX IF NOT EXISTS ix_break_glass_use_requests_created_at
    ON break_glass_use_requests (created_at);
CREATE INDEX IF NOT EXISTS ix_break_glass_use_requests_deleted_at
    ON break_glass_use_requests (deleted_at);
CREATE INDEX IF NOT EXISTS ix_break_glass_use_requests_cred_status
    ON break_glass_use_requests (credential_id, status);
""",
    ),
    (
        "auditaction.BREAK_GLASS_* values",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'BREAK_GLASS_REQUESTED';
""",
    ),
    (
        "auditaction.BREAK_GLASS_APPROVED value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'BREAK_GLASS_APPROVED';
""",
    ),
    (
        "auditaction.BREAK_GLASS_DENIED value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'BREAK_GLASS_DENIED';
""",
    ),
    (
        "auditaction.BREAK_GLASS_EXPIRED value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'BREAK_GLASS_EXPIRED';
""",
    ),
    (
        "auditaction.BREAK_GLASS_USED value",
        """
ALTER TYPE auditaction
    ADD VALUE IF NOT EXISTS 'BREAK_GLASS_USED';
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
        # one statement per semicolon — the batch may hold several
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.49 break-glass custody schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
