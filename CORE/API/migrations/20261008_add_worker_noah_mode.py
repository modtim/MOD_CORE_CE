"""worker-noah-mode — 24.5.s2: the SERVER-side lend/pause pin (no-large-sweep).

Creates worker_noah_mode (1:1 with worker_licenses):
  - mode  lend | pause | NULL (NULL = no pin, the automatic idle verdict rules)
  - until optional expiry on a pause ("pause lending until 18:00")

This is the server-side twin of the worker's LOCAL pin (CORE/Worker/internal/
availability/local_override.go + POST /noah/mode on its loopback UI), which
already exists and needs no storage. Same three-word vocabulary, so the
Dashboard control and the machine's own tray switch are two entry points into
ONE verdict rather than two availability machines.

Additive + inert: no row = no pin = today's behaviour, byte-identical.

Run inside the API container:
    docker exec my_fastapi_app python3 migrations/20261008_add_worker_noah_mode.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS worker_noah_mode (
    id BIGSERIAL PRIMARY KEY,
    worker_license_id INTEGER NOT NULL UNIQUE REFERENCES worker_licenses(id) ON DELETE CASCADE,
    mode VARCHAR(8) CHECK (mode IS NULL OR mode IN ('lend', 'pause', 'none')),
    until TIMESTAMP WITH TIME ZONE,
    set_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    set_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
"""


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
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

    if _table_exists(cur, "worker_noah_mode"):
        print("schema already current — nothing to do.")
        return 0

    print(f"{'WRITE' if args.commit else 'DRY  '}  create worker_noah_mode")
    if args.commit:
        cur.execute(CREATE_SQL)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
