"""Phase 3 — add CONTENT_RENAMED and CONTENT_MOVED to the auditaction enum.

The PATCH /cogs/{id} endpoint can rename (name change) and move
(parent_id change). Both deserve dedicated audit-action enum values
for compliance traceability (CMMC/ISO 27001 require structured audit
records, not free-text).

Run inside the API container:
    docker exec my_fastapi_app python3 migrations/20260425_add_content_rename_audit_actions.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ENUM_NAME = "auditaction"
# SQLAlchemy stores Python enum *names* (uppercase) in this column, not
# the values. Existing labels are CONTENT_DOWNLOAD / CONTENT_UPLOAD /
# etc., so the new entries must match that convention.
NEW_VALUES = ["CONTENT_RENAMED", "CONTENT_MOVED",
              "SUPPORT_CHAT_MESSAGE", "SUPPORT_CHAT_EMAILED"]


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


def _existing_values(cur) -> list[str]:
    cur.execute(
        """
        SELECT e.enumlabel
          FROM pg_type t
          JOIN pg_enum e ON t.oid = e.enumtypid
         WHERE t.typname = %s
         ORDER BY e.enumsortorder
        """,
        (ENUM_NAME,),
    )
    return [r[0] for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()
    current = _existing_values(cur)

    to_add = [v for v in NEW_VALUES if v not in current]
    if not to_add:
        print(f"All values already present — nothing to do.")
        return 0

    for v in to_add:
        stmt = f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{v}'"
        print(f"{'WRITE' if args.commit else 'DRY  '}  {stmt}")
        if args.commit:
            cur.execute(stmt)

    if args.commit:
        after = _existing_values(cur)
        print(f"Applied. {ENUM_NAME} now contains {len(after)} values "
              f"(added: {', '.join(to_add)}).")
    else:
        print("(dry run — re-run with --commit to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
