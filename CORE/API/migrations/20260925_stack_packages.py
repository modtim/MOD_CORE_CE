"""GF.8a — stack commerce tables (launch-completion-megaplan, gf-commerce).

Adds the three tables the unified catalog needs so customers can buy stacks
as well as workflows and nodes:

  stack_packages        — the immutable, version-pinned artifact a stack is
      sold as (images + weights + GPU requirements pinned to a
      persistent_stack_revision). One row per published VERSION; a new
      version is a new row, never a mutation. UNIQUE (slug, version).

  stack_listings        — the commercial wrapper (price, licence, visibility,
      publisher tenant) making a stack catalog-visible; mirrors
      workflow_listings so the catalog (GG.2) treats every kind the same way.

  tenant_stack_entitlements — record of a tenant owning a published
      StackListing; mirrors tenant_workflow_entitlements. A partial unique
      index over live rows (deleted_at IS NULL) keeps one ACTIVE entitlement
      per (tenant, listing) while allowing re-grant after soft revocation.

Purely additive — no existing table/data touched. Idempotent: CREATE
TABLE/INDEX IF NOT EXISTS; safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260925_stack_packages.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


CREATE_STACK_PACKAGES_SQL = """
CREATE TABLE IF NOT EXISTS stack_packages (
    id               SERIAL PRIMARY KEY,
    slug             VARCHAR(255) NOT NULL,
    name             VARCHAR(255) NOT NULL,
    version          VARCHAR(64) NOT NULL,
    revision_id      INTEGER REFERENCES persistent_stack_revision(id) ON DELETE SET NULL,
    image_digests    JSONB,
    weights          JSONB,
    gpu_requirements JSONB,
    license_class    VARCHAR(16) NOT NULL DEFAULT 'redistributable',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at       TIMESTAMPTZ,
    CONSTRAINT uq_stack_package_slug_version UNIQUE (slug, version)
);
"""

INDEX_STACK_PACKAGES_SLUG_SQL = """
CREATE INDEX IF NOT EXISTS ix_stack_packages_slug
    ON stack_packages (slug);
"""

INDEX_STACK_PACKAGES_REVISION_SQL = """
CREATE INDEX IF NOT EXISTS ix_stack_packages_revision_id
    ON stack_packages (revision_id);
"""

CREATE_STACK_LISTINGS_SQL = """
CREATE TABLE IF NOT EXISTS stack_listings (
    id                  SERIAL PRIMARY KEY,
    package_id          INTEGER NOT NULL REFERENCES stack_packages(id) ON DELETE CASCADE,
    publisher_tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name                VARCHAR(255) NOT NULL,
    description         TEXT,
    price_cents         INTEGER,
    price_tier          VARCHAR(20),
    licence             VARCHAR(20) NOT NULL DEFAULT 'open',
    status              VARCHAR(20) NOT NULL DEFAULT 'published',
    visibility          VARCHAR(20) NOT NULL DEFAULT 'public',
    allowed_tenant_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_listed           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);
"""

INDEX_STACK_LISTINGS_PACKAGE_SQL = """
CREATE INDEX IF NOT EXISTS ix_stack_listings_package_id
    ON stack_listings (package_id);
"""

INDEX_STACK_LISTINGS_PUBLISHER_SQL = """
CREATE INDEX IF NOT EXISTS ix_stack_listings_publisher_tenant_id
    ON stack_listings (publisher_tenant_id);
"""

INDEX_STACK_LISTINGS_DELETED_SQL = """
CREATE INDEX IF NOT EXISTS ix_stack_listings_deleted_at
    ON stack_listings (deleted_at);
"""

CREATE_TENANT_STACK_ENTITLEMENTS_SQL = """
CREATE TABLE IF NOT EXISTS tenant_stack_entitlements (
    id                 SERIAL PRIMARY KEY,
    tenant_id          INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    listing_id         INTEGER NOT NULL REFERENCES stack_listings(id) ON DELETE CASCADE,
    granted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    source             VARCHAR(20) NOT NULL DEFAULT 'admin_grant',
    expires_at         TIMESTAMPTZ,
    deleted_at         TIMESTAMPTZ
);
"""

INDEX_TENANT_STACK_ENTITLEMENTS_TENANT_SQL = """
CREATE INDEX IF NOT EXISTS ix_tenant_stack_entitlements_tenant_id
    ON tenant_stack_entitlements (tenant_id);
"""

INDEX_TENANT_STACK_ENTITLEMENTS_LISTING_SQL = """
CREATE INDEX IF NOT EXISTS ix_tenant_stack_entitlements_listing_id
    ON tenant_stack_entitlements (listing_id);
"""

INDEX_TENANT_STACK_ENTITLEMENTS_EXPIRES_SQL = """
CREATE INDEX IF NOT EXISTS ix_tenant_stack_entitlements_expires_at
    ON tenant_stack_entitlements (expires_at);
"""

# One ACTIVE entitlement per (tenant, listing); soft-deleted rows fall out
# of the index so a revoked entitlement can be re-granted.
INDEX_TENANT_STACK_ENTITLEMENTS_LIVE_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_stack_entitlement_live
    ON tenant_stack_entitlements (tenant_id, listing_id)
    WHERE deleted_at IS NULL;
"""

STATEMENTS = [
    CREATE_STACK_PACKAGES_SQL,
    INDEX_STACK_PACKAGES_SLUG_SQL,
    INDEX_STACK_PACKAGES_REVISION_SQL,
    CREATE_STACK_LISTINGS_SQL,
    INDEX_STACK_LISTINGS_PACKAGE_SQL,
    INDEX_STACK_LISTINGS_PUBLISHER_SQL,
    INDEX_STACK_LISTINGS_DELETED_SQL,
    CREATE_TENANT_STACK_ENTITLEMENTS_SQL,
    INDEX_TENANT_STACK_ENTITLEMENTS_TENANT_SQL,
    INDEX_TENANT_STACK_ENTITLEMENTS_LISTING_SQL,
    INDEX_TENANT_STACK_ENTITLEMENTS_EXPIRES_SQL,
    INDEX_TENANT_STACK_ENTITLEMENTS_LIVE_SQL,
]

TABLES = ["stack_packages", "stack_listings", "tenant_stack_entitlements"]


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
    # Create objects owned by modTech (not the transient Vault dynamic role
    # that runs this migration) so the tables match every other table's
    # ownership and the API's startup ALTER-OWNER reconcile is a no-op
    # instead of failing with "must be owner". The app's dynamic role is a
    # member of modTech, so SET ROLE succeeds; fall back to plain execution
    # if it can't (dev/sqlite).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for s in STATEMENTS:
        cur.execute(s)
    if set_role_ok:
        for t in TABLES:
            try:
                cur.execute(f'ALTER TABLE {t} OWNER TO {owner}')
            except Exception as e:  # noqa: BLE001
                print(f"(ALTER OWNER skipped for {t}: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("stack_packages, stack_listings and tenant_stack_entitlements tables ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
