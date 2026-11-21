"""Users — add is_service_account boolean + backfill the seeded api_service user.

Replaces the brittle ``current_user.username == "api_service"`` literal
check used by ``routers/node_definitions.py`` (and a couple of other
spots) to decide "is this caller the internal build-task / orchestrator
service?". The seeded service user in production carries
``username = 'api@service.local'`` (Keycloak-imported) — the literal
check has been silently false the whole time, so:

  - wizard-built tenant ``NodeDefinition`` rows landed as user-private
    to user_id=1 (the service user) instead of tenant-shared (user_id
    IS NULL). Result: nodes invisible to humans, ``/tenant/curate-list``
    missing the row, marketplace toggle blocked, workflow palette empty.
  - registrations fell back to ``current_user.tenant_id`` instead of
    honoring the spec's tenant_id. With one shared API key for all
    tenants, every tenant's build would have landed in tenant 1.

This migration:
  1. Adds ``users.is_service_account`` (BOOLEAN NOT NULL DEFAULT FALSE).
  2. Flips it TRUE for any user that owns a row in ``api_keys`` whose
     description matches the seeded service-account pattern. In stage
     today that's user id=1 (``api@service.local``) with the "Platform
     service account API key for internal calls" key. The match is
     case-insensitive on the substring ``service account`` to survive
     description rewording without code changes.

Idempotent. The ALTER is wrapped in IF NOT EXISTS; the UPDATE is a
no-op when the flag is already set.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260513_users_is_service_account.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    # 1. Column add (idempotent).
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'users'
           AND column_name = 'is_service_account'
        """
    )
    col_exists = cur.fetchone() is not None
    if col_exists:
        print("Column users.is_service_account already present.")
    else:
        if not args.commit:
            print("DRY  would: ALTER TABLE users ADD COLUMN is_service_account "
                  "BOOLEAN NOT NULL DEFAULT FALSE")
        else:
            cur.execute(
                "ALTER TABLE users "
                "ADD COLUMN is_service_account BOOLEAN NOT NULL DEFAULT FALSE"
            )
            print("WRITE ALTER TABLE users ADD COLUMN is_service_account "
                  "BOOLEAN NOT NULL DEFAULT FALSE")

    # 2. Identify candidate service-account users (preview).
    # Run the SELECT only when the column exists (post-commit, or pre-
    # existing). Skip the dry-run preview when the column hasn't been
    # created yet — the SELECT would error.
    if col_exists or args.commit:
        cur.execute(
            """
            SELECT DISTINCT u.id, u.username, u.email,
                   COALESCE(BOOL_OR(u.is_service_account), FALSE) AS already_set
              FROM users u
              JOIN api_keys k ON k.user_id = u.id
             WHERE k.deleted_at IS NULL
               AND LOWER(k.description) LIKE '%service account%'
             GROUP BY u.id, u.username, u.email
             ORDER BY u.id
            """
        )
        rows = cur.fetchall()
        if not rows:
            print("\nNo service-account candidates found in api_keys.")
        else:
            print(f"\nFound {len(rows)} candidate(s) to flag:")
            for uid, uname, email, already in rows:
                marker = "(already true)" if already else ""
                print(f"  user_id={uid}  username={uname!r}  email={email!r}  {marker}")

        if args.commit and rows:
            cur.execute(
                """
                UPDATE users
                   SET is_service_account = TRUE
                 WHERE id IN (
                     SELECT DISTINCT u.id
                       FROM users u
                       JOIN api_keys k ON k.user_id = u.id
                      WHERE k.deleted_at IS NULL
                        AND LOWER(k.description) LIKE '%service account%'
                 )
                   AND is_service_account = FALSE
                """
            )
            print(f"\nApplied. {cur.rowcount} row(s) updated.")

    if not args.commit:
        print("\n(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
