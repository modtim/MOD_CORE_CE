"""friendly-stack-fqdn (mesh-native-llm-routing D4) — record the per-stack
friendly public alias.

Adds `persistent_stacks.friendly_fqdn` (VARCHAR(255), nullable): an optional
human-friendly PUBLIC name (e.g. `app.example.com`) for an exposed
stack, aliased to the stack's stable internal mesh name (which is unchanged).
MOD provisions NO customer DNS — the customer points their own DNS at the MOD
edge; the ingress on-demand-TLS `ask` gate then authorises a per-host cert for
it. Set when exposed, cleared when not. Null = no friendly alias, legal on every
existing stack.

Run inside the API container:

    docker exec my_fastapi_app python3 migrations/20260826_stack_friendly_fqdn.py --commit

Idempotent — existence is tested before the ALTER (the Vault dynamic credential
means a later run is a different Postgres role, so IF NOT EXISTS is unreliable).
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


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if not _table_exists(cur, "persistent_stacks"):
        print("Table persistent_stacks absent — refusing to run.")
        return 1

    if _column_exists(cur, "persistent_stacks", "friendly_fqdn"):
        print("Column persistent_stacks.friendly_fqdn already present.")
    elif not args.commit:
        print("DRY RUN: would add persistent_stacks.friendly_fqdn VARCHAR(255)")
    else:
        cur.execute("ALTER TABLE persistent_stacks ADD COLUMN friendly_fqdn VARCHAR(255)")
        print("Added persistent_stacks.friendly_fqdn.")

    if not args.commit:
        print("\nDry run only. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
