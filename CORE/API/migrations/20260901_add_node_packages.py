"""node-ownership-purchases Phase 2 — node packages (bundles).

Creates node_packages, node_package_items, plan_packages. Additive and
behaviour-preserving: with no package rows nothing changes.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260901_add_node_packages.py --commit
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()
    steps: list[tuple[str, str]] = []

    if not _table_exists(cur, "node_packages"):
        steps.append((
            "create node_packages",
            """
            CREATE TABLE node_packages (
                id                SERIAL PRIMARY KEY,
                name              VARCHAR(150) NOT NULL,
                slug              VARCHAR(150),
                description       TEXT,
                tenant_id         INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
                price_cents       INTEGER,
                price_tier        VARCHAR(20),
                is_in_marketplace BOOLEAN NOT NULL DEFAULT false,
                created_at        TIMESTAMPTZ DEFAULT now()
            )
            """,
        ))
        steps.append(("index node_packages.slug",
                      "CREATE INDEX IF NOT EXISTS ix_node_packages_slug ON node_packages(slug)"))
        steps.append(("index node_packages.tenant_id",
                      "CREATE INDEX IF NOT EXISTS ix_node_packages_tenant_id ON node_packages(tenant_id)"))

    if not _table_exists(cur, "node_package_items"):
        steps.append((
            "create node_package_items",
            """
            CREATE TABLE node_package_items (
                id                 SERIAL PRIMARY KEY,
                package_id         INTEGER NOT NULL REFERENCES node_packages(id) ON DELETE CASCADE,
                node_definition_id INTEGER NOT NULL REFERENCES node_definitions(id) ON DELETE CASCADE,
                CONSTRAINT uq_node_package_item UNIQUE (package_id, node_definition_id)
            )
            """,
        ))
        steps.append(("index node_package_items.package_id",
                      "CREATE INDEX IF NOT EXISTS ix_node_package_items_package_id ON node_package_items(package_id)"))

    if not _table_exists(cur, "plan_packages"):
        steps.append((
            "create plan_packages",
            """
            CREATE TABLE plan_packages (
                id         SERIAL PRIMARY KEY,
                plan_id    INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                package_id INTEGER NOT NULL REFERENCES node_packages(id) ON DELETE CASCADE,
                CONSTRAINT uq_plan_package UNIQUE (plan_id, package_id)
            )
            """,
        ))
        steps.append(("index plan_packages.plan_id",
                      "CREATE INDEX IF NOT EXISTS ix_plan_packages_plan_id ON plan_packages(plan_id)"))

    if not steps:
        print("schema already current — nothing to do.")
    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        if args.commit:
            cur.execute(stmt)

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
