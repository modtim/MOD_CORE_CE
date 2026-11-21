"""sz-payg.4 (payg-billing-hardening tasks.md §1.4) — Tenant
auto-recharge + payg_setup columns for the PAYG billing hardening work.

Adds five additive columns to `tenants`:
  - auto_recharge_enabled           (BOOLEAN, default false)
  - auto_recharge_threshold_cents   (INTEGER, nullable)
  - auto_recharge_amount_cents      (INTEGER, nullable)
  - auto_recharge_payment_method_id (VARCHAR(255), nullable)
  - payg_setup_verified_at          (TIMESTAMPTZ, nullable)

`prepaid_balance_cents` (tasks.md §1.4) already shipped on `tenants`
(models.py:429, GB.PAY.1) — not touched here.

SHIPS SHADOW. These columns only EXPOSE storage for the auto-recharge
toggle/threshold/amount/payment-method and the payg SetupIntent
verification timestamp (sz-payg.14, sz-payg.18+); nothing reads or writes
them yet. No existing billing path changes behaviour. `tenants` is a hot
table — every column here is nullable or carries a constant (non-volatile)
DEFAULT, so this is a metadata-only change on PG >= 11 (no table rewrite,
no long lock).

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261009j_payg_tenant_autorecharge_columns.py --commit

Idempotent — existence is tested before each ALTER (the Vault dynamic
credential means a later run is a different Postgres role, so
IF NOT EXISTS is unreliable for columns).
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


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (table,))
    return cur.fetchone() is not None


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


# column name -> DDL type + default, applied only when the column is absent.
_COLUMNS = [
    ("auto_recharge_enabled", "BOOLEAN NOT NULL DEFAULT false"),
    ("auto_recharge_threshold_cents", "INTEGER"),
    ("auto_recharge_amount_cents", "INTEGER"),
    ("auto_recharge_payment_method_id", "VARCHAR(255)"),
    ("payg_setup_verified_at", "TIMESTAMPTZ"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if not _table_exists(cur, "tenants"):
        print("Table tenants absent — refusing to run.")
        return 1

    for column, ddl in _COLUMNS:
        if _column_exists(cur, "tenants", column):
            print(f"Column tenants.{column} already present.")
        elif not args.commit:
            print(f"DRY RUN: would add tenants.{column} ({ddl})")
        else:
            cur.execute(f"ALTER TABLE tenants ADD COLUMN {column} {ddl}")
            print(f"Added tenants.{column}.")

    if not args.commit:
        print("\nDry run only. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
