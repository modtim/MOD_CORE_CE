"""WS4.Q22 — notification_preferences: per-user, per-category, per-channel
notification delivery preference.

notification_preferences — one row per (user, category, channel):
  category   UI-facing grouping ('workflow' | 'billing' | 'product') the
             account-settings "Notification preferences" card shows a toggle
             for — see crud/notification_preferences.py:category_for_type for
             the mapping from a raw Notification.notification_type.
  channel    'in_app' | 'email'
  enabled    delivery on/off for that (category, channel). A MISSING row
             means enabled (the default) — the app never treats "no row" as
             disabled.

ISO 27001: soft-delete only (deleted_at); rows are never hard-deleted.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS; safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260929_notification_preferences.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_NOTIFICATION_PREFERENCES_SQL = """
CREATE TABLE IF NOT EXISTS notification_preferences (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category    VARCHAR(50) NOT NULL,
    channel     VARCHAR(20) NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ
);
"""

UQ_NOTIFICATION_PREFERENCES_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_pref_user_category_channel
    ON notification_preferences (user_id, category, channel);
"""

INDEX_NOTIFICATION_PREFERENCES_USER_SQL = """
CREATE INDEX IF NOT EXISTS ix_notification_preferences_user_id
    ON notification_preferences (user_id);
"""

STATEMENTS = [
    CREATE_NOTIFICATION_PREFERENCES_SQL,
    UQ_NOTIFICATION_PREFERENCES_SQL,
    INDEX_NOTIFICATION_PREFERENCES_USER_SQL,
]

TABLES = ["notification_preferences"]


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
        for s in STATEMENTS:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    # Create objects owned by modTech so the table matches every other
    # table's ownership and the API's startup ALTER-OWNER reconcile is a
    # no-op instead of failing with "must be owner". Fall back to plain
    # execution if SET ROLE isn't available (dev/sqlite).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for s in STATEMENTS:
        cur.execute(s)
    if set_role_ok:
        for t in TABLES:
            try:
                cur.execute(f'ALTER TABLE {t} OWNER TO {owner}')
            except Exception as e:  # noqa: BLE001
                print(f"(ALTER OWNER skipped for {t}: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("notification_preferences table ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
