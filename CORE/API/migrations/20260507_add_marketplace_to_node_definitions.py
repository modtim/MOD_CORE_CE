"""Marketplace M1 — add marketplace surface to node_definitions.

Two changes:
  1. ``is_in_marketplace`` BOOLEAN column on ``node_definitions``.
     Default FALSE — existing rows keep today's behaviour. When a
     platform admin flips it to TRUE on a system-shared node
     (tenant_id IS NULL), the node becomes visible in the tenant
     Marketplace surface but ONLY appears in a tenant's WorkflowBuilder
     palette after that tenant clicks "Download" (which inserts an
     entitlement row). Existing system nodes that aren't flagged
     stay auto-included in every tenant's palette — no behaviour
     change.
  2. ``tenant_node_entitlements`` join table — records which tenants
     have downloaded which marketplace nodes. Entitlement-based (not
     a row clone): a single platform-admin marketplace toggle
     cascades to every tenant's palette automatically.

Visibility semantics (post-migration):
  - System node, is_in_marketplace=FALSE: shows in all tenant palettes
    (today's behaviour, unchanged)
  - System node, is_in_marketplace=TRUE, no entitlement: hidden from
    tenant palette, visible in marketplace
  - System node, is_in_marketplace=TRUE, entitlement granted: visible
    in tenant palette
  - Tenant + private nodes: unaffected

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260507_add_marketplace_to_node_definitions.py --commit
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
        """
        SELECT 1 FROM information_schema.tables
         WHERE table_name = %s
        """,
        (table,),
    )
    return cur.fetchone() is not None


def _index_exists(cur, name: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM pg_indexes WHERE indexname = %s
        """,
        (name,),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    steps: list[tuple[str, str]] = []

    # Step 1 — add is_in_marketplace to node_definitions.
    if not _column_exists(cur, "node_definitions", "is_in_marketplace"):
        steps.append((
            "add is_in_marketplace column",
            "ALTER TABLE node_definitions "
            "ADD COLUMN is_in_marketplace BOOLEAN NOT NULL DEFAULT FALSE",
        ))
        # Partial index — only flagged rows, since the vast majority will
        # have is_in_marketplace=FALSE. Smaller index, faster scans for
        # the marketplace browse query (WHERE is_in_marketplace=TRUE).
        steps.append((
            "partial index on flagged marketplace nodes",
            "CREATE INDEX IF NOT EXISTS ix_node_definitions_marketplace "
            "ON node_definitions(id) WHERE is_in_marketplace = TRUE",
        ))

    # Step 2 — create tenant_node_entitlements join table.
    if not _table_exists(cur, "tenant_node_entitlements"):
        steps.append((
            "create tenant_node_entitlements table",
            """
            CREATE TABLE tenant_node_entitlements (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                node_definition_id INTEGER NOT NULL REFERENCES node_definitions(id) ON DELETE CASCADE,
                granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                granted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                CONSTRAINT uq_tenant_node_entitlement UNIQUE (tenant_id, node_definition_id)
            )
            """,
        ))
        steps.append((
            "index tenant_node_entitlements.tenant_id",
            "CREATE INDEX IF NOT EXISTS ix_tenant_node_entitlements_tenant "
            "ON tenant_node_entitlements(tenant_id)",
        ))
        steps.append((
            "index tenant_node_entitlements.node_definition_id",
            "CREATE INDEX IF NOT EXISTS ix_tenant_node_entitlements_node "
            "ON tenant_node_entitlements(node_definition_id)",
        ))

    if not steps:
        print("schema already current — nothing to do.")
        return 0

    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        print(f"        {stmt.strip()[:200]}")
        if args.commit:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
