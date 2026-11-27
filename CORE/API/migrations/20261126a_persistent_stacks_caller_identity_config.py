#!/usr/bin/env python3
"""identity-slice-2a — persistent_stacks.caller_identity_config (nullable JSONB).

Per the design (the internal design notes
section 1), stacks may opt in per-service to passing caller identity claims.
The column holds a nullable JSONB map:
    {<service>: {"identity_claim": true, "on_behalf_of": true}}

This migration adds the column to `persistent_stacks` (the table already exists
from stack creation). It is idempotent (IF NOT EXISTS), nullable, no default,
no backfill.

Run: python3 migrations/20261126a_persistent_stacks_caller_identity_config.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_conn():
    """Return an autocommit connection to the live Postgres DB."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = getattr(raw, "driver_connection", None) or getattr(raw, "connection", None) or raw
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--commit", action="store_true", help="Actually run DDL (default = dry-run)")
    args = p.parse_args()

    statements = [
        "ALTER TABLE persistent_stacks ADD COLUMN IF NOT EXISTS caller_identity_config JSONB",
    ]

    if not args.commit:
        for stmt in statements:
            print(f"{stmt};")
        print(f"(dry-run; {len(statements)} statement(s), nothing executed; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    applied = 0
    for stmt in statements:
        cur.execute(stmt)
        applied += 1
    cur.close()
    print(f"persistent_stacks.caller_identity_config: {applied} column(s) ensured.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
