"""Add tone-classification columns for the support-chat deescalation
feature. See the internal design notes.

  * support_chat_messages.tone
      VARCHAR(16) NULL — per-message tone label (HAPPY / NEUTRAL /
      FRUSTRATED / ANGRY / ABUSIVE). NULL for pre-migration rows and
      for assistant rows (only user messages carry tone).

  * support_chat_sessions.frustration_score
      INTEGER NOT NULL DEFAULT 0 — running aggregate. Threshold-only
      comparator, not a user-visible value.

  * support_chat_sessions.consecutive_apologies
      INTEGER NOT NULL DEFAULT 0 — counts consecutive assistant turns
      that contained "sorry" / "apologise". Caps at APOLOGY_CAP via
      the prompt directive.

  * support_chat_sessions.auto_escalated_at
      TIMESTAMPTZ NULL — set when should_auto_escalate flips. The UI
      reads this to show the "we've flagged this for our team"
      banner and the existing escalation pipeline picks up the
      transcript by session id.

Run inside the API container:
    docker exec my_fastapi_app python3 migrations/20260518_support_chat_tone.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


_DDL = [
    """
    ALTER TABLE support_chat_messages
        ADD COLUMN IF NOT EXISTS tone VARCHAR(16);
    """,
    """
    ALTER TABLE support_chat_sessions
        ADD COLUMN IF NOT EXISTS frustration_score INTEGER NOT NULL DEFAULT 0;
    """,
    """
    ALTER TABLE support_chat_sessions
        ADD COLUMN IF NOT EXISTS consecutive_apologies INTEGER NOT NULL DEFAULT 0;
    """,
    """
    ALTER TABLE support_chat_sessions
        ADD COLUMN IF NOT EXISTS auto_escalated_at TIMESTAMPTZ;
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_support_chat_sessions_auto_escalated_at
        ON support_chat_sessions (auto_escalated_at)
        WHERE auto_escalated_at IS NOT NULL;
    """,
]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
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

    if not args.commit:
        print("DRY  would execute:")
        for stmt in _DDL:
            print(stmt.strip()[:80] + "…")
        print("\n(dry run — re-run with --commit)")
        return 0

    for stmt in _DDL:
        cur.execute(stmt)
        print(f"  ✓ {stmt.strip()[:80]}…")
    print("\nMigration applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
