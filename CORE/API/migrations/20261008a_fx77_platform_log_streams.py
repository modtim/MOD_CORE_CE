#!/usr/bin/env python3
"""GSEC FX.77 s2 — the ONE platform-level (tenant-less) log stream.

FX.77 s2 (owner-approved existing-column change) adds a platform/system
event stream that belongs to no tenant. It reuses `tenant_log_streams`
rather than a new table, so `tenant_id` becomes NULLABLE and a NULL
tenant_id means "the platform stream".

    tenant_log_streams (EXISTING table — column change only, NO new table)
      tenant_id  INTEGER -> NULL   FK tenants(id) retained: a NULL row
                                   simply belongs to no tenant.

Constraint added (the DB enforces "there is only one", not just the
service):

    uq_tenant_log_streams_platform_live   partial UNIQUE on (name) WHERE
                                   tenant_id IS NULL AND deleted_at IS
                                   NULL — at most ONE LIVE platform stream.
                                   The WHERE clause excludes tombstones, so
                                   soft-deleting the platform stream (ISO
                                   27001 no-hard-delete) and re-creating it
                                   never collides with its own soft-deleted
                                   row. The existing
                                   uq_tenant_log_streams_tenant_name UNIQUE
                                   (tenant_id, name) is untouched: NULL
                                   never collides in a Postgres UNIQUE, so
                                   it never constrained the platform row.

NO DROP / DELETE / TRUNCATE anywhere — the column is relaxed, nothing is
removed, and the FK to tenants(id) is kept as-is.

Idempotent: ALTER ... DROP NOT NULL is a no-op once applied, CREATE
UNIQUE INDEX IF NOT EXISTS likewise. Dry run (default, no --commit) PRINTS
the SQL and exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261008a_fx77_platform_log_streams.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261008a_fx77_platform_log_streams.py --commit   # apply

NOT applied by the s2 task — the lead applies it with --commit.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "tenant_log_streams"


_STATEMENTS = [
    (
        f"{_TABLE}.tenant_id nullable (FX.77 s2 platform stream)",
        f"""
ALTER TABLE {_TABLE} ALTER COLUMN tenant_id DROP NOT NULL;
""",
    ),
    (
        f"uq_{_TABLE}_platform_live partial unique index",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_platform_live
    ON {_TABLE} (name) WHERE tenant_id IS NULL AND deleted_at IS NULL;
""",
    ),
]


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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        for stmt in (s.strip() for s in sql.split(";") if s.strip()):
            try:
                cur.execute(stmt)
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {label}: {e}")
                return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "FX.77 s2 ready: tenant_log_streams.tenant_id is nullable and "
        "uq_tenant_log_streams_platform_live keeps at most one LIVE "
        "platform stream."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
