#!/usr/bin/env python3
"""fortress-4.5 (S) -- ADD node_run_attestations.attested_region / attested_storage.

revision      : 20261013f_fortress_45_region_storage
down_revision : 20261013e_wsip_run_events_composite_index

Fortress 4.5 slice S (compliance-tag enforcement, 4.4): the platform must be
able to prove that the worker which executed a tagged workflow's node ran in an
ALLOWED region and wrote its outputs to COMPLIANT storage.  ``node_run_attestations``
(20261012x_fortress_23c_node_run_attestations.py) is the durable per-task
attestation row, but it carries no region/storage columns -- the values today
only exist inside the free-form ``contribution`` JSONB, which cannot be indexed
or compared cheaply and is never parsed by the enforcement layer (ISO rule:
never ``.astext`` on JSONB).

Columns (additive, both NULLABLE TEXT, NO default, NO CHECK):

  - attested_region  TEXT NULL -- the region the worker declared for this
    execution (free text: the worker's own region label, matched against the
    tenant's allowed-region policy by the 4.4 enforcement slice).
  - attested_storage TEXT NULL -- the storage target the worker wrote outputs
    to (free text: matched against the tenant's compliant-storage policy).

Nullable with no default is the point: an older worker that does not yet send
region/storage keeps NULL, and NULL means "not attested" -- which the 4.4
enforcement layer treats as UNSATISFIED (see tests/test_fortress_45_run_attestation_service.py
attested-worker-but-unattested-region), never as "allowed".  No CHECK here so
no existing row can be rejected and no vocabulary is frozen in stone.

The table is APPEND-ONLY (fortress 2.3c): the shared ``fortress_13_append_only_guard``
trigger pair from 20261012q/20261012x already refuses UPDATE/DELETE/TRUNCATE on
it.  Adding a column does NOT touch that guard, so this migration re-declares
nothing about it -- purely ALTER TABLE ADD COLUMN.

``id`` stays ``BIGSERIAL PRIMARY KEY`` from the original table -- never a
max()-subquery DEFAULT (Postgres rejects subqueries in a DEFAULT clause), and
there is no ON DELETE clause in this migration at all (no FK added); where an
FK cascade is ever written it is ``ON DELETE SET NULL`` (two words), never
``ONDELETE``.

Idempotent: ADD COLUMN IF NOT EXISTS for both columns, so a re-run is a no-op.
Additive only.

Downgrade (DEV-ONLY path): drops the two columns.  Prod never runs downgrade --
ISO 27001's no-hard-delete rule governs ROWS, not schema; dropping an additive,
nullable column set on a dev/test DB is not a row-destruction event.

DRY-RUN by default; --commit applies. NEVER applied to the live DB by the lane
-- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261013f_fortress_45_region_storage.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "node_run_attestations"

_UPGRADE_STATEMENTS = [
    (
        f"{_TABLE}.attested_region TEXT NULL",
        f"""
ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS
    attested_region TEXT;
""",
    ),
    (
        f"{_TABLE}.attested_storage TEXT NULL",
        f"""
ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS
    attested_storage TEXT;
""",
    ),
]

_DOWNGRADE_STATEMENTS = [
    (
        "drop column attested_storage",
        f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS attested_storage;",
    ),
    (
        "drop column attested_region",
        f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS attested_region;",
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
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the ALTER steps. Without this flag, dry-run only.")
    p.add_argument("--downgrade", action="store_true",
                   help="DEV-ONLY: drop the two columns instead of adding them.")
    args = p.parse_args()

    statements = _DOWNGRADE_STATEMENTS if args.downgrade else _UPGRADE_STATEMENTS

    if not args.commit:
        for label, sql in statements:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        if not args.downgrade:
            print("DRY  downgrade: dev-only, drops the two columns added here "
                  "(prod never runs downgrade -- ISO 27001's no-hard-delete "
                  "rule governs rows, not schema)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in statements:
        try:
            cur.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    if args.downgrade:
        print("fortress-4.5 S downgrade applied: node_run_attestations.attested_"
              "region/attested_storage removed (dev-only).")
    else:
        print(
            "fortress-4.5 S ready: node_run_attestations carries nullable "
            "attested_region / attested_storage (append-only guard from "
            "fortress 1.3 untouched)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
