"""FX.117 PART 2 (Q104=A) — per-secret workflow scope + optional expiry.

Adds four nullable columns to `secrets` (additive, idempotent — every
ALTER is guarded so re-running is a no-op):

  allowed_workflow_ids  JSONB            NULL = any workflow of the
                                         tenant (current behaviour);
                                         when set, the by-name run-token
                                         injection path only serves runs
                                         of these workflows.
  expires_at            TIMESTAMPTZ      NULL = no expiry; in the past,
                                         run-time injection refuses the
                                         secret (403 + audited). The row
                                         is NEVER hard-deleted (ISO
                                         27001: soft state only).
  expiry_reminders_sent JSONB            [day, ...] — which reminder
                                         days already fired (dedupe for
                                         the daily 04:32 UTC sweep).
  expired_notified_at   TIMESTAMPTZ      the sweep's soft "expired"
                                         stamp + one-time owner notice.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261005n_fx117_secret_scope_and_expiry.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

COLUMNS = [
    ("allowed_workflow_ids", "JSONB"),
    ("expires_at", "TIMESTAMPTZ"),
    ("expiry_reminders_sent", "JSONB"),
    ("expired_notified_at", "TIMESTAMPTZ"),
]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        for name, typ in COLUMNS:
            print(
                f"ALTER TABLE secrets ADD COLUMN IF NOT EXISTS "
                f"{name} {typ};"
            )
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    for name, typ in COLUMNS:
        cur.execute(
            f"ALTER TABLE secrets ADD COLUMN IF NOT EXISTS "
            f"{name} {typ}"
        )
        print(f"secrets.{name} {typ}: OK")
    cur.close()
    print(f"Added {len(COLUMNS)} FX.117 PART 2 columns to secrets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
