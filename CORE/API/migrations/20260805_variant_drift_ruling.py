"""project-agent §1.4/§1.5 — a drifted variant is ruled on, not defaulted.

A non-hero variant scoring below ``DRIFT_THRESHOLD`` against its reference
got verdict ``drift`` and published anyway, with a warning chip in the
node config panel. That assumed a human would see the chip. Variant
selection happens by placement (``routers/worker_tasks.py`` intersects
``gpu_variants`` with the worker's hardware tags) and has never consulted
a sample-test verdict, so nothing stopped user work routing to the
divergent build.

Schema (all on ``node_variant_verification``):

  - ``drift_ruling`` VARCHAR(16) — NULL means awaiting a ruling, and an
    awaiting variant is NOT selected to serve work. Otherwise
    ``publish`` / ``drop`` / ``keep_fixing``.
  - ``drift_ruled_at`` TIMESTAMPTZ
  - ``drift_ruled_by_user_id`` INTEGER FK → users(id)

Backfill: existing ``drift`` rows are set to ``publish``, because that is
what the platform has been doing for them already. Making them awaiting
would retroactively pull variants out of service that tenants are using
right now, which is a behaviour change nobody asked for. New drift
verdicts start awaiting (or follow the tenant's
``node_build.drift_default`` setting).

Run inside the API container:

    docker exec my_fastapi_app python3 \\
        migrations/20260805_variant_drift_ruling.py --commit

Idempotent — repeated runs are safe.
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


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return cur.fetchone() is not None


_COLUMNS = [
    ("drift_ruling", "VARCHAR(16)"),
    ("drift_ruled_at", "TIMESTAMPTZ"),
    ("drift_ruled_by_user_id", "INTEGER REFERENCES users(id)"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if not _table_exists(cur, "node_variant_verification"):
        print("Table node_variant_verification absent — run "
              "20260523_variant_verification.py first.")
        return 1

    fresh = False
    for name, ddl in _COLUMNS:
        if _column_exists(cur, "node_variant_verification", name):
            print(f"Column node_variant_verification.{name} already present.")
            continue
        stmt = f"ALTER TABLE node_variant_verification ADD COLUMN {name} {ddl}"
        if not args.commit:
            print(f"DRY  would: {stmt}")
        else:
            cur.execute(stmt)
            print(f"WRITE {stmt}")
        fresh = True

    # Grandfather what is already in service. These variants are running
    # user work today; flipping them to awaiting would take them out of
    # service on deploy.
    backfill = (
        "UPDATE node_variant_verification SET drift_ruling = 'publish' "
        "WHERE verdict = 'drift' AND drift_ruling IS NULL"
    )
    if not args.commit:
        print("DRY  would: grandfather existing drift rows to "
              "drift_ruling='publish'")
    elif fresh or _column_exists(cur, "node_variant_verification",
                                 "drift_ruling"):
        cur.execute(backfill)
        print(f"WRITE grandfathered {cur.rowcount} existing drift row(s) "
              f"to 'publish'")

    if args.commit:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_nvv_node_def_variant ON "
            "node_variant_verification(node_definition_id, variant) "
            "WHERE node_definition_id IS NOT NULL"
        )
        print("WRITE CREATE INDEX IF NOT EXISTS idx_nvv_node_def_variant")
    else:
        print("DRY  would: CREATE INDEX idx_nvv_node_def_variant "
              "(dispatch reads this per task)")

    print("Done." if args.commit else "Dry run complete. Re-run with --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
