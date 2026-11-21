"""GG.UI.8c — Core half of the admin catalog work.

Two new, unrelated-but-shipped-together tables:

  plan_listing_includes   generalized plan -> catalog-listing inclusion
                          (kind IN node/stack/workflow/bundle), the admin
                          plan-include editor's backing store. Soft-delete
                          only (deleted_at/deleted_by_user_id) — ISO 27001,
                          no hard deletes. `PlanIncludedNode` (node-only)
                          and `PlanPackage` (package/bundle-only) predate
                          the unified GG.2 catalog and are untouched; this
                          table is the new admin surface for the general
                          case, see models.PlanListingInclude's docstring.

  listing_coupon_defs      a coupon that targets ONE catalog listing
                          (target_kind + target_ref_id) and either grants
                          it free or applies a %/fixed discount to that
                          listing only. Redeemed automatically inside
                          services/closure_fulfilment.apply_listing_coupon
                          at /catalog/{kind}/{id}/buy-with-credits and
                          /checkout. Mirrors the existing bonus_coupon_defs
                          "active=False to retire" soft-delete style.

ISO 27001: rows are only ever soft-deleted / deactivated, never removed.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS; safe to re-run.

NOT APPLIED YET — the lead applies it. When running:
    docker exec my_fastapi_app python3 \\
        migrations/20260928_plan_listing_includes_and_coupons.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_PLAN_LISTING_INCLUDES_SQL = """
CREATE TABLE IF NOT EXISTS plan_listing_includes (
    id                  SERIAL PRIMARY KEY,
    plan_id             INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    kind                VARCHAR(20) NOT NULL
                        CHECK (kind IN ('node', 'stack', 'workflow', 'bundle')),
    ref_id              INTEGER NOT NULL,
    created_by_user_id  INTEGER REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    deleted_by_user_id  INTEGER REFERENCES users(id)
);
"""

# GG.UI.8d — the uniqueness guarantee must be PARTIAL on (plan_id, kind,
# ref_id) WHERE deleted_at IS NULL. A plain table UNIQUE constraint would
# make re-including a listing after a soft-delete 500 (the soft-deleted row
# still occupies the (plan_id, kind, ref_id) slot). Only ACTIVE includes are
# unique; soft-deleted rows may stack up (ISO 27001 — never hard-deleted).
# Installs that already ran the first version of this migration have a FULL
# unique constraint of the same name; drop it so the partial index below can
# take the name (no-op on fresh installs and on re-runs).
DROP_PLAN_LISTING_INCLUDES_FULL_UQ_SQL = """
ALTER TABLE plan_listing_includes DROP CONSTRAINT IF EXISTS uq_plan_listing_include;
"""

INDEX_PLAN_LISTING_INCLUDES_PARTIAL_UQ_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_listing_include
    ON plan_listing_includes (plan_id, kind, ref_id)
    WHERE deleted_at IS NULL;
"""

INDEX_PLAN_LISTING_INCLUDES_PLAN_SQL = """
CREATE INDEX IF NOT EXISTS ix_plan_listing_includes_plan_id
    ON plan_listing_includes (plan_id);
"""

INDEX_PLAN_LISTING_INCLUDES_KIND_REF_SQL = """
CREATE INDEX IF NOT EXISTS ix_plan_listing_includes_kind_ref
    ON plan_listing_includes (kind, ref_id);
"""

INDEX_PLAN_LISTING_INCLUDES_DELETED_SQL = """
CREATE INDEX IF NOT EXISTS ix_plan_listing_includes_deleted_at
    ON plan_listing_includes (deleted_at);
"""

CREATE_LISTING_COUPON_DEFS_SQL = """
CREATE TABLE IF NOT EXISTS listing_coupon_defs (
    id                  SERIAL PRIMARY KEY,
    name                VARCHAR(80) NOT NULL,
    code                VARCHAR(40) NOT NULL UNIQUE,
    target_kind         VARCHAR(20) NOT NULL
                        CHECK (target_kind IN ('node', 'stack', 'workflow', 'bundle')),
    target_ref_id       INTEGER NOT NULL,
    discount_type       VARCHAR(16) NOT NULL
                        CHECK (discount_type IN ('free', 'percent', 'fixed')),
    discount_value      INTEGER,
    max_redemptions     INTEGER,
    times_redeemed      INTEGER NOT NULL DEFAULT 0,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_by_user_id  INTEGER REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_listing_coupon_discount_value CHECK (
        (discount_type = 'free' AND discount_value IS NULL) OR
        (discount_type = 'percent' AND discount_value > 0 AND discount_value <= 100) OR
        (discount_type = 'fixed' AND discount_value > 0)
    )
);
"""

INDEX_LISTING_COUPON_DEFS_CODE_SQL = """
CREATE INDEX IF NOT EXISTS ix_listing_coupon_defs_code
    ON listing_coupon_defs (code);
"""

INDEX_LISTING_COUPON_DEFS_TARGET_SQL = """
CREATE INDEX IF NOT EXISTS ix_listing_coupon_defs_target
    ON listing_coupon_defs (target_kind, target_ref_id);
"""

STATEMENTS = [
    CREATE_PLAN_LISTING_INCLUDES_SQL,
    DROP_PLAN_LISTING_INCLUDES_FULL_UQ_SQL,
    INDEX_PLAN_LISTING_INCLUDES_PARTIAL_UQ_SQL,
    INDEX_PLAN_LISTING_INCLUDES_PLAN_SQL,
    INDEX_PLAN_LISTING_INCLUDES_KIND_REF_SQL,
    INDEX_PLAN_LISTING_INCLUDES_DELETED_SQL,
    CREATE_LISTING_COUPON_DEFS_SQL,
    INDEX_LISTING_COUPON_DEFS_CODE_SQL,
    INDEX_LISTING_COUPON_DEFS_TARGET_SQL,
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
        for s in STATEMENTS:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    # Create the new objects owned by modTech (not the transient Vault dynamic
    # role that runs this migration) so ownership matches every other object
    # and the API's startup ALTER-OWNER reconcile stays a no-op. The app's
    # dynamic role is a member of modTech, so SET ROLE succeeds; fall back to
    # plain execution if it can't (dev/sqlite).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for s in STATEMENTS:
        cur.execute(s)
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("plan_listing_includes + listing_coupon_defs ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
