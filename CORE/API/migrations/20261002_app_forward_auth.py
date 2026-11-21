"""GX.S1 — app forward-auth config (require_login / allowed_groups /
public_paths).

Adds a single JSONB column ``forward_auth_config`` to the ``apps`` table.
The whole per-app forward-auth shape lives in ONE column (documented key
list below) so adding a future key (e.g. an allowed user-type list for
GX.S1b) is a config change, not another migration:

    {
        "require_login":  true,          # false ⇒ no forward_auth at all
        "allowed_groups": ["ops"],       # KC group names; empty = any authed user
        "public_paths":   ["/healthz"]   # path prefixes that skip forward_auth
    }

Defaults are applied in code (services/app_forward_auth.py) — a NULL column
means "use the defaults" (require_login=false, no groups, no public paths),
so existing rows need no backfill.

Additive only; idempotent: ALTER TABLE ... ADD COLUMN IF NOT EXISTS. Safe
to re-run.

LEAD APPLIES — not applied by the task agent (models.py is bind-mounted
live; applying belongs to the deploy step).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261002_app_forward_auth.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLES = ()

ADD_FORWARD_AUTH_CONFIG_SQL = """
ALTER TABLE apps
    ADD COLUMN IF NOT EXISTS forward_auth_config JSONB;
"""

_STATEMENTS = [
    ("apps += forward_auth_config", ADD_FORWARD_AUTH_CONFIG_SQL),
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
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        for t in _TABLES:
            try:
                cur.execute(f'ALTER TABLE {t} OWNER TO {owner}')
            except Exception as e:  # noqa: BLE001
                print(f"(ALTER TABLE {t} OWNER TO {owner} failed: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("app forward-auth config column ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
