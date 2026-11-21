"""noah-next-free-at — per-worker "next free at" hint (WS3.26).

Adds (additive, behaviour-preserving):
  - worker_licenses.noah_next_free_at (TIMESTAMPTZ, nullable)
    The worker's self-reported earliest time it expects to be free again.
    NULL = no hint.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260926_add_noah_next_free_at.py --commit
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


def _col(cur, table, col) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (table, col),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = _get_conn()
    cur = conn.cursor()

    adds = [
        ("worker_licenses", "noah_next_free_at", "TIMESTAMPTZ"),
    ]
    steps = [
        (f"add {t}.{c}", f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {c} {ddl}")
        for (t, c, ddl) in adds if not _col(cur, t, c)
    ]

    if not steps:
        print("schema already current — nothing to do.")
    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        if args.commit:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
