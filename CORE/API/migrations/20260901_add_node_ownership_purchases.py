"""node-ownership-purchases — one-time node ownership: pricing, purchase ledger,
entitlement source discriminator.

Creates `node_purchases` and adds:
  - node_definitions.price_cents, price_tier         (one-time price on a node)
  - tenant_node_entitlements.source                  (plan_grant|purchase|admin_grant)
  - tenant_node_entitlements.purchase_id             (FK -> node_purchases)

Backfill (idempotent, behaviour-preserving):
  - every existing entitlement row → source='admin_grant' (preserves today's
    free access; the reconciler only ever touches 'plan_grant' rows).

This migration does NOT price any node (price_cents stays NULL) — with all
prices NULL the marketplace behaves exactly as today. Charging begins only when
an admin sets price_cents on a system+marketplace node.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260901_add_node_ownership_purchases.py --commit
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()
    steps: list[tuple[str, str]] = []

    # 1. node_purchases table
    if not _table_exists(cur, "node_purchases"):
        steps.append((
            "create node_purchases",
            """
            CREATE TABLE node_purchases (
                id                        SERIAL PRIMARY KEY,
                tenant_id                 INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                node_definition_id        INTEGER REFERENCES node_definitions(id) ON DELETE SET NULL,
                package_id                INTEGER,
                amount_cents              INTEGER NOT NULL,
                currency                  VARCHAR(3) NOT NULL DEFAULT 'usd',
                stripe_payment_intent_id  VARCHAR(255) UNIQUE,
                purchased_by_user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
                refunded_at               TIMESTAMPTZ
            )
            """,
        ))
        steps.append((
            "index node_purchases.tenant_id",
            "CREATE INDEX IF NOT EXISTS ix_node_purchases_tenant_id ON node_purchases(tenant_id)",
        ))
        steps.append((
            "index node_purchases.node_definition_id",
            "CREATE INDEX IF NOT EXISTS ix_node_purchases_node_definition_id ON node_purchases(node_definition_id)",
        ))

    # 1b. plan_included_nodes (plan -> free node mapping)
    if not _table_exists(cur, "plan_included_nodes"):
        steps.append((
            "create plan_included_nodes",
            """
            CREATE TABLE plan_included_nodes (
                id                 SERIAL PRIMARY KEY,
                plan_id            INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                node_definition_id INTEGER NOT NULL REFERENCES node_definitions(id) ON DELETE CASCADE,
                CONSTRAINT uq_plan_included_node UNIQUE (plan_id, node_definition_id)
            )
            """,
        ))
        steps.append((
            "index plan_included_nodes.plan_id",
            "CREATE INDEX IF NOT EXISTS ix_plan_included_nodes_plan_id ON plan_included_nodes(plan_id)",
        ))

    # 2. node_definitions pricing columns
    if not _column_exists(cur, "node_definitions", "price_cents"):
        steps.append((
            "add node_definitions.price_cents",
            "ALTER TABLE node_definitions ADD COLUMN price_cents INTEGER",
        ))
    if not _column_exists(cur, "node_definitions", "price_tier"):
        steps.append((
            "add node_definitions.price_tier",
            "ALTER TABLE node_definitions ADD COLUMN price_tier VARCHAR(20)",
        ))

    # 3. tenant_node_entitlements discriminator + purchase link
    if not _column_exists(cur, "tenant_node_entitlements", "source"):
        steps.append((
            "add tenant_node_entitlements.source",
            "ALTER TABLE tenant_node_entitlements "
            "ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'admin_grant'",
        ))
    if not _column_exists(cur, "tenant_node_entitlements", "purchase_id"):
        steps.append((
            "add tenant_node_entitlements.purchase_id",
            "ALTER TABLE tenant_node_entitlements "
            "ADD COLUMN purchase_id INTEGER REFERENCES node_purchases(id) ON DELETE SET NULL",
        ))
        steps.append((
            "index tenant_node_entitlements.purchase_id",
            "CREATE INDEX IF NOT EXISTS ix_tenant_node_entitlements_purchase_id "
            "ON tenant_node_entitlements(purchase_id)",
        ))

    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        if args.commit:
            cur.execute(stmt)
    if not steps:
        print("schema already current — checking backfill...")

    # 4. Backfill: the column default already stamps 'admin_grant' on new+existing
    #    rows at ADD time, but an explicit UPDATE covers a column added earlier
    #    without the default. Idempotent.
    backfill = (
        "UPDATE tenant_node_entitlements SET source = 'admin_grant' "
        "WHERE source IS NULL"
    )
    can_backfill = args.commit or _column_exists(cur, "tenant_node_entitlements", "source")
    if can_backfill:
        print(f"{'WRITE' if args.commit else 'DRY  '}  backfill entitlement source -> admin_grant (NULLs only)")
        if args.commit:
            cur.execute(backfill)
            print(f"        rows affected: {cur.rowcount}")

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
