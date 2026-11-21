"""launch-day-followup-5 §1A — extend tenant_email_configs for the
OpenBao-backed Postmark migration, and add User.email_bounce_flag for
bounce-flagged user surfacing.

Additive columns on `tenant_email_configs`:
  - password_vault_path VARCHAR(255)        — OpenBao KV v2 path under
    secret/data/<path>. When set, mailer reads SMTP password from OpenBao.
    When NULL, mailer falls back to encrypted_smtp_password (preserves
    any pre-migration per-tenant SMTP config the codebase still has).
  - last_tested_at TIMESTAMPTZ              — last time the test-send
    button was clicked.
  - last_test_result VARCHAR(32)            — 'success' / 'failed' / NULL.
  - last_test_error TEXT                    — failure reason on failed test.

Additive column on `users`:
  - email_bounce_flag BOOLEAN NOT NULL DEFAULT FALSE — set by the
    Postmark bounce webhook when a user accumulates ≥3 bounces in 30d.
    Surfaces in the platform-admin Users tab; admin can clear after
    investigation.

Idempotent — `IF NOT EXISTS` everywhere. Safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 migrations/20260506_add_email_password_vault_path.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ADD_PASSWORD_VAULT_PATH_SQL = """
ALTER TABLE tenant_email_configs
    ADD COLUMN IF NOT EXISTS password_vault_path VARCHAR(255)
"""

ADD_LAST_TESTED_AT_SQL = """
ALTER TABLE tenant_email_configs
    ADD COLUMN IF NOT EXISTS last_tested_at TIMESTAMP WITH TIME ZONE
"""

ADD_LAST_TEST_RESULT_SQL = """
ALTER TABLE tenant_email_configs
    ADD COLUMN IF NOT EXISTS last_test_result VARCHAR(32)
"""

ADD_LAST_TEST_ERROR_SQL = """
ALTER TABLE tenant_email_configs
    ADD COLUMN IF NOT EXISTS last_test_error TEXT
"""

ADD_USER_BOUNCE_FLAG_SQL = """
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email_bounce_flag BOOLEAN NOT NULL DEFAULT FALSE
"""


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


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name=%s AND column_name=%s LIMIT 1
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--commit",
        action="store_true",
        help="Apply the changes. Without this flag, dry-run only.",
    )
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    needs = {
        "password_vault_path": not _column_exists(cur, "tenant_email_configs", "password_vault_path"),
        "last_tested_at": not _column_exists(cur, "tenant_email_configs", "last_tested_at"),
        "last_test_result": not _column_exists(cur, "tenant_email_configs", "last_test_result"),
        "last_test_error": not _column_exists(cur, "tenant_email_configs", "last_test_error"),
        "email_bounce_flag": not _column_exists(cur, "users", "email_bounce_flag"),
    }

    if not any(needs.values()):
        print("All columns already present — nothing to do.")
        return 0

    for col, missing in needs.items():
        if missing:
            print(f"  column missing: {col}")

    if not args.commit:
        print("(dry run — re-run with --commit to apply)")
        return 0

    if needs["password_vault_path"]:
        print("  ALTER TABLE tenant_email_configs ADD COLUMN password_vault_path ...")
        cur.execute(ADD_PASSWORD_VAULT_PATH_SQL)
    if needs["last_tested_at"]:
        print("  ALTER TABLE tenant_email_configs ADD COLUMN last_tested_at ...")
        cur.execute(ADD_LAST_TESTED_AT_SQL)
    if needs["last_test_result"]:
        print("  ALTER TABLE tenant_email_configs ADD COLUMN last_test_result ...")
        cur.execute(ADD_LAST_TEST_RESULT_SQL)
    if needs["last_test_error"]:
        print("  ALTER TABLE tenant_email_configs ADD COLUMN last_test_error ...")
        cur.execute(ADD_LAST_TEST_ERROR_SQL)
    if needs["email_bounce_flag"]:
        print("  ALTER TABLE users ADD COLUMN email_bounce_flag ...")
        cur.execute(ADD_USER_BOUNCE_FLAG_SQL)

    # Verify
    final = {
        "password_vault_path": _column_exists(cur, "tenant_email_configs", "password_vault_path"),
        "last_tested_at": _column_exists(cur, "tenant_email_configs", "last_tested_at"),
        "last_test_result": _column_exists(cur, "tenant_email_configs", "last_test_result"),
        "last_test_error": _column_exists(cur, "tenant_email_configs", "last_test_error"),
        "email_bounce_flag": _column_exists(cur, "users", "email_bounce_flag"),
    }
    missing = [k for k, present in final.items() if not present]
    if missing:
        print(f"ERROR: columns still missing after ALTER: {missing}", file=sys.stderr)
        return 1

    print("Applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
