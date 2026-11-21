"""stack-secrets-and-certs §5 — record the per-stack TLS declaration.

Adds `persistent_stacks.tls_config` (JSONB, nullable): the per-service TLS
config parsed from the compose spec's x-mod.tls fields —
{service: {mode: auto|byo, fqdn, not_after?}}. Declaration + parsed byo expiry
only; the byo cert+key themselves live in Vault, never here. Null = nothing
declared, legal on every existing stack.

Run inside the API container:

    docker exec my_fastapi_app python3 migrations/20260824_stack_tls_config.py --commit

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

    if _column_exists(cur, "persistent_stacks", "tls_config"):
        print("Column persistent_stacks.tls_config already present.")
    elif not args.commit:
        print("DRY RUN: would add persistent_stacks.tls_config JSONB")
    else:
        cur.execute("ALTER TABLE persistent_stacks ADD COLUMN tls_config JSONB")
        print("Added persistent_stacks.tls_config.")

    if not args.commit:
        print("\nDry run only. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
