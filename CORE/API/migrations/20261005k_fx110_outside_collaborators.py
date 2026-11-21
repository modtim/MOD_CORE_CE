"""FX.110 (owner decision Q97=A) — outside collaborators: lifecycle columns.

Every external account and every external grant gets an end date; the
default is 90 days (tenant-scoped platform setting
``external_collab.default_expiry_days``, services/platform_settings.py),
the inviter is reminded 7 days before (deduped), and a daily scheduler
job soft-disables expired access — never a hard delete.

This migration adds the columns models.py declares:

users:
    external_access_expires_at          end date for the external account
                                        (stamped from the invitation expiry
                                        on accept)
    external_expiry_reminded_at         dedupe stamp for the 7-day-before
                                        reminder to the inviter
    external_terms_accepted_at          terms/NDA gate acceptance moment
                                        (audit row carries the version hash)
    external_profile_approved_by_user_id
                                        gov/DoD (cmmc / fedramp_high_il5)
                                        per-user tenant-admin approval

resource_permissions:
    external_restriction_options        JSONB
                                        {"view_only", "no_download",
                                         "forced_watermark"} per external
                                        grant (NULL = none). The grant end
                                        date reuses the existing expires_at
                                        column — no duplicate added.

All columns are additive (ADD COLUMN IF NOT EXISTS) and nullable with
correct NULL semantics ("not an external account" / "not set"), so
existing rows need no backfill: an internal user keeps
external_access_expires_at NULL, and a pre-existing external grant keeps
its expires_at where one was set (external-invitation grants already
carry the invitation expiry).

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order, recorded in the append-only
release_steps ledger) — this file sorts after 20261005j and before
20261005l.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261005k_fx110_outside_collaborators.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261005k_fx110_outside_collaborators.py --commit   # apply

models.py is bind-mounted live into the API container, so the columns
must exist before any FX.110 code path touches them — apply immediately
after adding the columns to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "users.external_access_expires_at column",
        """
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS external_access_expires_at TIMESTAMP WITH TIME ZONE;
""",
    ),
    (
        "users.external_expiry_reminded_at column",
        """
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS external_expiry_reminded_at TIMESTAMP WITH TIME ZONE;
""",
    ),
    (
        "users.external_terms_accepted_at column",
        """
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS external_terms_accepted_at TIMESTAMP WITH TIME ZONE;
""",
    ),
    (
        "users.external_profile_approved_by_user_id column",
        """
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS external_profile_approved_by_user_id INTEGER;
""",
    ),
    # PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS — guard via a DO
    # block on pg_constraint (idempotent on re-run).
    (
        "users.external_profile_approved_by_user_id FK",
        """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_users_external_profile_approved_by'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT fk_users_external_profile_approved_by
            FOREIGN KEY (external_profile_approved_by_user_id)
            REFERENCES users (id);
    END IF;
END
$$;
""",
    ),
    (
        "resource_permissions.external_restriction_options column",
        """
ALTER TABLE resource_permissions
    ADD COLUMN IF NOT EXISTS external_restriction_options JSONB;
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
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.110 outside-collaborator columns ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
