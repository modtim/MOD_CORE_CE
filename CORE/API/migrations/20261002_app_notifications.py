"""GX.S8 (launch-completion-megaplan Group X) — app notification fan-out.

Adds two ADDITIVE nullable columns to the existing `notifications` table
so apps can fan a notification out via POST /v1/notifications (in-app +
email in this edition). Reuses the existing `message` / `notification_type`
/ `link_url` columns for the body text, the type, and the link — no other
column is needed.

  severity    info | warning | error | critical
  dedupe_key  caller-supplied idempotency key; a second fan-out with the
              same key for the same tenant reports `deduped: true` instead
              of creating duplicate rows.

A new `AuditAction` member `APP_NOTIFICATION_SENT` is appended; the PG
`auditaction` enum stores member NAMES, so it is added with
`ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'APP_NOTIFICATION_SENT'`.

Purely additive — no existing table/data touched. Idempotent (ADD COLUMN
IF NOT EXISTS / ALTER TYPE ... IF NOT EXISTS). Safe to re-run.

Limits (max recipients per call / per-minute rate) are DB platform
settings (services.platform_settings, group "notifications") — NOT env;
this migration seeds no tunable values, the registry defaults apply until
an operator overrides them.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261002_app_notifications.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ("notifications",)

ADD_SEVERITY_SQL = (
    "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS severity "
    "VARCHAR(20)"
)
ADD_DEDUPE_KEY_SQL = (
    "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS dedupe_key "
    "VARCHAR(255)"
)
INDEX_DEDUPE_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_notifications_dedupe_key "
    "ON notifications (dedupe_key)"
)
# AuditAction is a PG enum (`auditaction`) storing member NAMES.
ENUM_APP_NOTIFICATION_SENT_SQL = (
    "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'APP_NOTIFICATION_SENT'"
)

_STATEMENTS = [
    ("notifications.severity", ADD_SEVERITY_SQL),
    ("notifications.dedupe_key", ADD_DEDUPE_KEY_SQL),
    ("index notifications(dedupe_key)", INDEX_DEDUPE_SQL),
    ("auditaction += APP_NOTIFICATION_SENT", ENUM_APP_NOTIFICATION_SENT_SQL),
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
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
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
    print("app notification columns ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
