"""delegated-stack-authoring Group 1 — the platform-capability grant table.

Adds `platform_capabilities`: a direct, platform-scoped least-privilege grant of
a named capability (e.g. "stack:author") to a user, WITHOUT conferring full
is_platform_admin. Distinct from the cog Permission system (resource-scoped) and
from the `capabilities` table (the node/resource action kind).

A grant is ACTIVE when revoked_at IS NULL and (expires_at IS NULL OR
expires_at > now()). Revocation sets revoked_at (non-destructive) so the audit
trail survives; re-granting inserts a new row.

Run inside the API container:

    docker exec my_fastapi_app python3 migrations/20260821_platform_capabilities.py --commit

Idempotent — existence is tested before create (the Vault dynamic credential
means a later run is a DIFFERENT Postgres role, so IF NOT EXISTS is unreliable
because it checks ownership first).
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


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (table,))
    return cur.fetchone() is not None


def _index_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (name,))
    return cur.fetchone() is not None


_TABLE = """
CREATE TABLE platform_capabilities (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    capability          VARCHAR(100) NOT NULL,
    granted_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    granted_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    revoked_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL
)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if not _table_exists(cur, "users"):
        print("Table users absent — refusing to run.")
        return 1

    if _table_exists(cur, "platform_capabilities"):
        print("Table platform_capabilities already present.")
    elif not args.commit:
        print("DRY RUN: would CREATE TABLE platform_capabilities")
    else:
        cur.execute(_TABLE)
        print("Created platform_capabilities.")

    idx = "ix_platform_capability_user_cap"
    if _index_exists(cur, idx):
        print(f"Index {idx} already present.")
    elif not args.commit:
        print(f"DRY RUN: would create index {idx}")
    else:
        cur.execute(f"CREATE INDEX {idx} ON platform_capabilities (user_id, capability)")
        print(f"Created index {idx}.")

    if not args.commit:
        print("\nDry run only. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
