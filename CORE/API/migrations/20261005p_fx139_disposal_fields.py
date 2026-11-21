"""FX.139 part 3 — disposal-record columns on the FX.126 asset inventory.

docs/isms/14-media-sanitisation-disposal-policy.md ("Disposal record")
requires every sanitisation/disposal event to be recorded against the
asset-inventory row: date/time, asset serial, method, certificate
reference. FX.126 already stores per-asset metadata as columns on
worker_licenses (owner_name / data_class / site_region), so the disposal
record lives next to them as four more nullable columns:

worker_licenses:
    disposal_serial          VARCHAR(255)  NULL  serial at disposal
    disposal_date            TIMESTAMPTZ   NULL  when sanitised/destroyed
    disposal_method          VARCHAR(40)   NULL  crypto_erase | physical_destroy
                                               | crypto_erase_then_destroy
                                               | returned_keep_drive
    disposal_certificate_ref TEXT          NULL  certificate id or URL (not a file)

Additive + nullable: every pre-existing worker keeps NULL ("never
sanitised") — exactly the pre-FX.139 behaviour. No backfill.

ISO 27001: nothing is deleted; the values are set exclusively through
the audited PATCH /v1/admin/assets/workers/{worker_name} endpoint
(same ASSET_WORKER_METADATA_UPDATED audit row as the owner/data-class
edits).

Migration runner is tools/mod_migrate.py (steps discovered as *.py in
this directory, run in filename order, recorded in the append-only
release_steps ledger) — this file sorts after 20261005o.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261005p_fx139_disposal_fields.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \
        migrations/20261005p_fx139_disposal_fields.py --commit   # apply

models.py is bind-mounted live into the API container, so the columns
must exist before any FX.139 code path touches them — apply immediately
after adding them to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "worker_licenses.disposal_serial column",
        """
ALTER TABLE worker_licenses
    ADD COLUMN IF NOT EXISTS disposal_serial VARCHAR(255);
""",
    ),
    (
        "worker_licenses.disposal_date column",
        """
ALTER TABLE worker_licenses
    ADD COLUMN IF NOT EXISTS disposal_date TIMESTAMPTZ;
""",
    ),
    (
        "worker_licenses.disposal_method column",
        """
ALTER TABLE worker_licenses
    ADD COLUMN IF NOT EXISTS disposal_method VARCHAR(40);
""",
    ),
    (
        "worker_licenses.disposal_certificate_ref column",
        """
ALTER TABLE worker_licenses
    ADD COLUMN IF NOT EXISTS disposal_certificate_ref TEXT;
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
    print("FX.139 disposal-record columns ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
