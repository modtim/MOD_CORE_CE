"""Bonus coupons — extra-credits / extra-storage perks for a fixed duration.

Two new tables:

  bonus_coupon_defs       admin-managed templates (code, kind, value,
                          duration_months, max_redemptions)
  tenant_bonus_coupons    per-tenant redemptions
                          (snapshot of kind/value + starts_at/ends_at)

The credit grant query and the storage cap query both sum the active
tenant_bonus_coupons rows (where now() between starts_at and ends_at)
and add the result to the tenant's plan-baseline allotment.

Stripe-native money discounts (% off / $ off) are handled by Stripe
Coupons + PromotionCodes via the existing checkout flow — see
routers/admin_plans_payment_settings.create_coupon. This migration is
only for the app-level bonus model that Stripe doesn't support natively.

Run inside the API container:
    docker exec my_fastapi_app python3 migrations/20260514_bonus_coupons.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


_DDL = [
    """
    CREATE TABLE IF NOT EXISTS bonus_coupon_defs (
        id                  SERIAL PRIMARY KEY,
        name                VARCHAR(80) NOT NULL,
        code                VARCHAR(40) NOT NULL UNIQUE,
        kind                VARCHAR(16) NOT NULL
                            CHECK (kind IN ('credits', 'storage')),
        value               INTEGER NOT NULL CHECK (value > 0),
        duration_months     INTEGER NOT NULL
                            CHECK (duration_months > 0 AND duration_months <= 120),
        max_redemptions     INTEGER,
        times_redeemed      INTEGER NOT NULL DEFAULT 0,
        active              BOOLEAN NOT NULL DEFAULT TRUE,
        created_by_user_id  INTEGER REFERENCES users(id),
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_bonus_coupon_defs_code
        ON bonus_coupon_defs (code);
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_bonus_coupons (
        id                      SERIAL PRIMARY KEY,
        tenant_id               INTEGER NOT NULL REFERENCES tenants(id),
        bonus_coupon_def_id     INTEGER NOT NULL REFERENCES bonus_coupon_defs(id),
        kind                    VARCHAR(16) NOT NULL,
        value                   INTEGER NOT NULL,
        starts_at               TIMESTAMPTZ NOT NULL,
        ends_at                 TIMESTAMPTZ NOT NULL,
        redeemed_by_user_id     INTEGER REFERENCES users(id),
        created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_tenant_bonus_coupons_tenant_id
        ON tenant_bonus_coupons (tenant_id);
    """,
    """
    -- Composite (tenant_id, ends_at) index — partial predicate
    -- `WHERE ends_at > NOW()` would be ideal but NOW() isn't
    -- IMMUTABLE, so Postgres rejects it. Full index is fine at this
    -- table's expected scale (~one row per tenant per redemption).
    CREATE INDEX IF NOT EXISTS ix_tenant_bonus_coupons_active
        ON tenant_bonus_coupons (tenant_id, ends_at);
    """,
]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    if not args.commit:
        print("DRY  would execute:")
        for stmt in _DDL:
            print(stmt.strip()[:80] + "…")
        print("\n(dry run — re-run with --commit)")
        return 0

    for stmt in _DDL:
        cur.execute(stmt)
        print(f"  ✓ {stmt.strip()[:80]}…")
    print("\nMigration applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
