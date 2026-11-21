"""clean-v2-api Wave 10 (tier-enforcement gap #1) — plans.tier

Adds an OPTIONAL commercial-tier column to `plans`: one of
the entitlement bundle keys utils.entitlements defines (this edition's tiers). When set, `entitlements_from_plan()` grants that tier's whole
Cap.* bundle (mesh federation, burst, predictive, GPU-pool mgmt, multi-tenant,
SCIM, ...) on top of the plan's existing 5 boolean feature columns — bringing
SaaS Plans up to the same capability vocabulary CORE/LicenseServer's on-prem
TIER_PRESETS already express, per clean-v2-api Wave 10 "CE POLICY RESOLVED:
Reconciled".

Purely additive and NULL-default: every existing plan gets `tier = NULL`,
which `entitlements_from_plan` treats as "no bundle" — no plan's effective
entitlements change until a platform admin explicitly sets `tier` on it
(Admin -> Plans, or a follow-up data migration naming known plans by name, deliberately NOT done here to avoid silently widening any
existing paying customer's grant).

Idempotent: IF NOT EXISTS column add; safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260923_add_plan_tier.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


ADD_COLUMN_SQL = """
ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS tier VARCHAR(20);
"""


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
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()
    if not args.commit:
        print(ADD_COLUMN_SQL)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(ADD_COLUMN_SQL)
    cur.close()
    print("plans.tier added (NULL default; no existing plan's entitlements changed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
