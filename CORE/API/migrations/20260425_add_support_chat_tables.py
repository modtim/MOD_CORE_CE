"""Phase 4 — create support_chat_sessions + support_chat_messages.

Multi-turn conversation persistence for the unified support LLM.
Sessions split by kind="general" vs kind="logs" so each gets its own
prompt + retrieval scope; cross-link via parent_session_id when the
user promotes a logs finding into a general thread.

Run inside the API container:
    docker exec my_fastapi_app python3 migrations/20260425_add_support_chat_tables.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS support_chat_sessions (
    id VARCHAR(36) PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind VARCHAR(16) NOT NULL DEFAULT 'general',
    task_id VARCHAR(64),
    parent_session_id VARCHAR(36) REFERENCES support_chat_sessions(id) ON DELETE SET NULL,
    title VARCHAR(200),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_message_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_support_sessions_user ON support_chat_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_support_sessions_tenant ON support_chat_sessions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_support_sessions_task ON support_chat_sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_support_sessions_last_msg ON support_chat_sessions(last_message_at);
"""

CREATE_MESSAGES_SQL = """
CREATE TABLE IF NOT EXISTS support_chat_messages (
    id BIGSERIAL PRIMARY KEY,
    session_id VARCHAR(36) NOT NULL REFERENCES support_chat_sessions(id) ON DELETE CASCADE,
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    sources_json JSON,
    token_usage JSON,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_support_messages_session ON support_chat_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_support_messages_created ON support_chat_messages(created_at);
"""


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine None")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name=%s LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    actions: list[tuple[str, str]] = []
    if not _table_exists(cur, "support_chat_sessions"):
        actions.append(("support_chat_sessions", CREATE_SESSIONS_SQL))
    if not _table_exists(cur, "support_chat_messages"):
        actions.append(("support_chat_messages", CREATE_MESSAGES_SQL))

    if not actions:
        print("Both tables already exist — nothing to do.")
        return 0

    for name, sql in actions:
        print(f"{'WRITE' if args.commit else 'DRY  '}  CREATE {name}")
        if args.commit:
            cur.execute(sql)

    if args.commit:
        for name, _ in actions:
            if not _table_exists(cur, name):
                print(f"ERROR: {name} not present after CREATE", file=sys.stderr)
                return 1
        print("Applied.")
    else:
        print("(dry run — re-run with --commit to apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
