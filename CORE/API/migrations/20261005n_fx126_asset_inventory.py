"""FX.126 (owner decision Q113=A) — platform asset inventory metadata.

The admin asset register (GET /v1/admin/assets) carries, per worker:
a named owner, a data classification and a site/region. Those are
admin-set metadata about the PHYSICAL ASSET, not enrollment state, so
they live on the worker record (worker_licenses) rather than on
worker_enrollments (which re-enrollment recreates rows for).

This migration adds the three nullable columns models.py declares:

worker_licenses:
    owner_name   VARCHAR(255)  NULL  named asset owner
    data_class   VARCHAR(16)   NULL  public | internal | confidential | cui
    site_region  VARCHAR(100)  NULL  e.g. "dc-east", "nyc-office"

Additive + nullable: every pre-existing worker keeps NULL ("not
classified yet") — exactly the pre-FX.126 behaviour. No backfill.

ISO 27001: nothing is deleted; the values are set exclusively through
the audited PATCH /v1/admin/assets/workers/{worker_name} endpoint.

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order, recorded in the append-only
release_steps ledger) — this file sorts after 20261005m.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261005n_fx126_asset_inventory.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261005n_fx126_asset_inventory.py --commit   # apply

models.py is bind-mounted live into the API container, so the columns
must exist before any FX.126 code path touches them — apply immediately
after adding them to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "worker_licenses.owner_name column",
        """
ALTER TABLE worker_licenses
    ADD COLUMN IF NOT EXISTS owner_name VARCHAR(255);
""",
    ),
    (
        "worker_licenses.data_class column",
        """
ALTER TABLE worker_licenses
    ADD COLUMN IF NOT EXISTS data_class VARCHAR(16);
""",
    ),
    (
        "worker_licenses.site_region column",
        """
ALTER TABLE worker_licenses
    ADD COLUMN IF NOT EXISTS site_region VARCHAR(100);
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
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.126 asset inventory columns ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
