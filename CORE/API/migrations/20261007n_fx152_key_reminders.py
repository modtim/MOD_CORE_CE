"""GSEC FX.152 part 3 (owner decision Q139=B) — API-key reminder email state.

No new table: the per-key reminder dedupe flags already created by
20261005j_fx114_service_accounts.py are the sent-state, and the weekly stale
digest reuses the existing `lifecycle_email_log` table (unique
(tenant_id, email_key), email_key = 'fx152_stale_digest:<iso-week>').

This migration is the idempotent safety net for the two flags — it re-asserts
them for any install where the FX.114 step was skipped (MOD CE builds run a
reduced step set), and adds the `expires_at` index the daily reminder sweep
filters on. Everything is `IF NOT EXISTS`, so applying it twice is a no-op and
existing rows keep their values (no defaults, no backfill).

    api_keys.reminder_14d_sent BOOLEAN NOT NULL DEFAULT false
    api_keys.reminder_3d_sent  BOOLEAN NOT NULL DEFAULT false
    api_keys.expires_at        index (CREATE INDEX IF NOT EXISTS)

Run on an autocommit connection (same pattern as 20261007a/20261007h).

Dry run (default, no --commit) PRINTS the SQL and exits 0 without touching the
database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261007n_fx152_key_reminders.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261007n_fx152_key_reminders.py --commit   # apply

Read by services/api_key_reminders.py (the daily reminder + weekly digest
scheduler jobs). Nothing here disables a key.
"""
from __future__ import annotations

import argparse
import os
import sys


_STATEMENTS = [
    ("api_keys.reminder_14d_sent column",
     """
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS reminder_14d_sent BOOLEAN
    NOT NULL DEFAULT false;
"""),
    ("api_keys.reminder_3d_sent column",
     """
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS reminder_3d_sent BOOLEAN
    NOT NULL DEFAULT false;
"""),
    ("api_keys.expires_at reminder-sweep index",
     """
CREATE INDEX IF NOT EXISTS ix_api_keys_expires_at
    ON api_keys (expires_at);
"""),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="apply the statements instead of printing them")
    args = parser.parse_args()

    if not args.commit:
        print("# DRY RUN — migrations/20261007n_fx152_key_reminders.py")
        for label, sql in _STATEMENTS:
            print(f"-- {label}")
            print(sql.strip())
            print()
        return 0

    import psycopg2

    # CE-MIGROLE-1: no hardcoded owner/user default — connect as whatever
    # POSTGRES_USER this install uses (SaaS: the modTech superuser; CE fresh
    # boot: `core`).  Nothing here re-owns objects, so no ALTER OWNER role
    # token is needed; the statements are all IF-NOT-EXISTS column/index DDL.
    user = os.environ.get("POSTGRES_USER")
    password = os.environ.get("POSTGRES_PASSWORD")
    db = os.environ.get("POSTGRES_DB")
    if not (user and password and db):
        raise RuntimeError(
            "POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB env vars "
            "missing — run this migration inside my_fastapi_app where "
            "they're populated by docker-compose."
        )

    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres-app"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=db,
        user=user,
        password=password,
    )
    conn.autocommit = True
    cur = conn.cursor()
    for label, sql in _STATEMENTS:
        cur.execute(sql)
        print(f"applied: {label}")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
