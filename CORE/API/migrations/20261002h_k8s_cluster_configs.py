"""GT.D8.2 (launch-completion-megaplan) — tenant k8s cluster registry.

Adds `k8s_cluster_configs`: one row per tenant Kubernetes cluster the
k8s backend may dispatch to (see models.K8sClusterConfig). The gate
(services/k8s_cluster_registry) counts ACTIVE rows per tenant against
the entitlement tier: CE (Cap.K8S_BACKEND) allows exactly one — or an
env-only deployment (KUBECONFIG / in-cluster ServiceAccount, no rows),
which the registry counts as ONE implicit cluster; 2+ active clusters
require Cap.K8S_MULTI_CLUSTER (Business).

Security: `kubeconfig_vault_ref` stores ONLY the OpenBao path of the
kubeconfig secret — never the kubeconfig itself. `in_cluster` clusters
need no ref (the API's ServiceAccount is used).

Additive + idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT
EXISTS — a second run is a no-op. Soft-delete only (ISO 27001): rows
are tombstoned via deleted_at, never physically deleted; the partial
unique index (tenant_id, name WHERE deleted_at IS NULL) frees a name
immediately on soft-delete so it can be recreated.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. The lead applies with --commit.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261002h_k8s_cluster_configs.py            # dry run: prints SQL
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261002h_k8s_cluster_configs.py --commit   # apply

LEAD APPLIES — not applied by the task agent.
"""
from __future__ import annotations

import argparse
import os
import sys

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS k8s_cluster_configs (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    name VARCHAR(255) NOT NULL,
    kubeconfig_vault_ref VARCHAR(512),
    in_cluster BOOLEAN NOT NULL DEFAULT false,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    deleted_at TIMESTAMP WITH TIME ZONE
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_k8s_cluster_configs_tenant_id
    ON k8s_cluster_configs (tenant_id);
CREATE INDEX IF NOT EXISTS ix_k8s_cluster_configs_deleted_at
    ON k8s_cluster_configs (deleted_at);
-- One live name per tenant; soft-deleted tombstones do not squat the name.
CREATE UNIQUE INDEX IF NOT EXISTS uq_k8s_cluster_configs_live_tenant_name
    ON k8s_cluster_configs (tenant_id, name)
    WHERE deleted_at IS NULL;
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
    statements = [CREATE_TABLE_SQL, INDEX_SQL]
    if not args.commit:
        for s in statements:
            print(s)
        print("(dry-run; pass --commit to apply)")
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — re-own to the current user
    for s in statements:
        cur.execute(s)
    cur.execute(f'ALTER TABLE k8s_cluster_configs OWNER TO {_owner_role.owner_role(cur)};')
    cur.close()
    print("k8s_cluster_configs ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
