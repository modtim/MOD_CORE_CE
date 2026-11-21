"""delegated-stack-authoring Group 4 — record the per-stack egress posture.

Adds `persistent_stacks.secret_refs` (JSONB, nullable): the per-service egress
posture parsed from the compose spec's x-mod.secrets fields —
{service: {mode: none|api|hosts, hosts: [...]}}. RECORDED at create; the worker
does not yet enforce it (Group 5). Null = nothing declared, legal until the
Group 6 flip.

Run inside the API container:

    docker exec my_fastapi_app python3 migrations/20260822_stack_secret_refs.py --commit

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

    if _column_exists(cur, "persistent_stacks", "secret_refs"):
        print("Column persistent_stacks.secret_refs already present.")
    elif not args.commit:
        print("DRY RUN: would add persistent_stacks.secret_refs JSONB")
    else:
        cur.execute("ALTER TABLE persistent_stacks ADD COLUMN secret_refs JSONB")
        print("Added persistent_stacks.secret_refs.")

    if not args.commit:
        print("\nDry run only. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
