"""FX.102 (owner Q88=A) — tenant share-link policy: download counter.

Adds the `download_count` column to `shareable_links` so the tenant
setting `share.download_cap` (services/platform_settings.py, group
"share") can meter downloads per link. `visit_count` (link opens)
already exists and is untouched; the cap bounds DOWNLOADS, not opens,
so the two counters stay separate.

Column mirrors models.ShareableLink.download_count exactly
(Integer, default 0 — existing rows read as 0 = no downloads yet,
which is the correct answer for every pre-existing link).

The repo's migration runner is `tools/mod_migrate.py`, not Alembic:
steps are discovered as `*.py` files in this directory and run in
filename order, each recorded in the append-only `release_steps`
ledger. Naming convention `YYYYMMDD[_letter].slug.py` — this file
sorts after 20261005h.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit.

Run inside the API container:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261005i_fx102_share_link_policy.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261005i_fx102_share_link_policy.py --commit   # apply

Additive only: ALTER TABLE ADD COLUMN IF NOT EXISTS — nothing is
dropped or truncated.
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "shareable_links"

COLUMN_SQL = f"""
ALTER TABLE {_TABLE}
    ADD COLUMN IF NOT EXISTS download_count INTEGER NOT NULL DEFAULT 0;
"""

_STATEMENTS = [
    (f"{_TABLE}.download_count column", COLUMN_SQL),
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
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print(f"{_TABLE}.download_count ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
