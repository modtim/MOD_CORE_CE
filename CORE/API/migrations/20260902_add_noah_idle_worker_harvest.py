"""noah-idle-worker-harvest — opportunistic idle-harvest settings.

Adds (all nullable/additive, behaviour-preserving):
  - worker_licenses.noah_enabled / noah_sustained_quiet_s / noah_pause_grace_s
    / noah_plugged_in_only / noah_schedule_start / noah_schedule_end
    / noah_batch_appetite / noah_available / noah_reason  (NULL = inherit)
  - platform_noah_settings singleton (id=1) platform default, seeded OFF.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260902_add_noah_idle_worker_harvest.py --commit
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


def _table(cur, table) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name=%s",
        (table,),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = _get_conn()
    cur = conn.cursor()

    adds = [
        ("worker_licenses", "noah_enabled", "BOOLEAN"),
        ("worker_licenses", "noah_sustained_quiet_s", "INTEGER"),
        ("worker_licenses", "noah_pause_grace_s", "INTEGER"),
        ("worker_licenses", "noah_plugged_in_only", "BOOLEAN"),
        ("worker_licenses", "noah_schedule_start", "VARCHAR(5)"),
        ("worker_licenses", "noah_schedule_end", "VARCHAR(5)"),
        ("worker_licenses", "noah_batch_appetite", "INTEGER"),
        ("worker_licenses", "noah_available", "BOOLEAN"),
        ("worker_licenses", "noah_reason", "VARCHAR(64)"),
        # noah-observability: last availability flip + cumulative harvest count.
        ("worker_licenses", "noah_available_since", "TIMESTAMPTZ"),
        ("worker_licenses", "noah_harvested_total", "INTEGER NOT NULL DEFAULT 0"),
    ]
    steps = [
        (f"add {t}.{c}", f"ALTER TABLE {t} ADD COLUMN {c} {ddl}")
        for (t, c, ddl) in adds if not _col(cur, t, c)
    ]

    if not _table(cur, "platform_noah_settings"):
        steps.append((
            "create platform_noah_settings",
            """
            CREATE TABLE platform_noah_settings (
                id INTEGER PRIMARY KEY DEFAULT 1,
                enabled BOOLEAN NOT NULL DEFAULT false,
                sustained_quiet_s INTEGER NOT NULL DEFAULT 300,
                pause_grace_s INTEGER NOT NULL DEFAULT 120,
                plugged_in_only BOOLEAN NOT NULL DEFAULT true,
                schedule_start VARCHAR(5),
                schedule_end VARCHAR(5),
                batch_appetite INTEGER NOT NULL DEFAULT 0,
                stale_release_s INTEGER NOT NULL DEFAULT 75,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_by_user_id INTEGER REFERENCES users(id)
            )
            """,
        ))
        steps.append((
            "seed platform_noah_settings row",
            "INSERT INTO platform_noah_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
        ))

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
