"""ISO 27001 no-hard-delete — soft-delete columns on the admin/plans-pricing tables.

Adds `deleted_at TIMESTAMPTZ NULL` to the nine tables backing the admin
plans-and-pricing tool family, so their DELETE endpoints can tombstone a
row (set `deleted_at`, plus `is_active=false` where the table has that
flag) instead of hard-deleting it. The routers were migrated to filter
`deleted_at IS NULL` out of every read path in the same change.

Tables touched:
  - plans                     (Plan)
  - currencies                (Currency)
  - geographic_regions        (GeographicRegion)
  - regional_pricing          (RegionalPricing — soft-cascaded with its region)
  - infrastructure_costs      (InfrastructureCost)
  - tenant_pricing_overrides  (TenantPricingOverride)
  - promo_codes               (PromoCode)
  - seasonal_pricing_schedules(SeasonalPricingSchedule)
  - webhook_endpoints         (WebhookEndpoint)

Unlike the wizard soft-delete tables, these models are NOT enrolled in the
global `with_loader_criteria` filter in database.py (that hook uses an
explicit model list). Live-row filtering is done explicitly in the
admin_plans_* routers, so this migration only adds the storage column plus
a partial `WHERE deleted_at IS NULL` index for the live-row read path.

Idempotent: every step uses IF NOT EXISTS.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260919_add_soft_delete_admin_plans.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


SOFT_DELETE_TABLES = (
    "plans",
    "currencies",
    "geographic_regions",
    "regional_pricing",
    "infrastructure_costs",
    "tenant_pricing_overrides",
    "promo_codes",
    "seasonal_pricing_schedules",
    "webhook_endpoints",
)


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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    steps: list[tuple[str, str]] = []

    for table in SOFT_DELETE_TABLES:
        steps.append((
            f"add {table}.deleted_at",
            f"ALTER TABLE {table} "
            f"ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL",
        ))
        # Partial index — only live rows are indexed, so the router's
        # `deleted_at IS NULL` reads hit a tiny B-tree even after years of
        # tombstone accumulation.
        ix_name = f"ix_{table}_live"
        steps.append((
            f"partial index {ix_name}",
            f"CREATE INDEX IF NOT EXISTS {ix_name} ON {table} (id) "
            f"WHERE deleted_at IS NULL",
        ))

    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        print(f"        {stmt.strip()[:200]}")
        if args.commit:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
