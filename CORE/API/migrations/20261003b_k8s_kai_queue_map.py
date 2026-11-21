"""18.3 (GI.3) — KAI queue admin mapping on registered clusters.

Adds `kai_queue_map` (JSONB, nullable) to `k8s_cluster_configs`
(20261002h): the admin override map of tenant/project -> EXISTING KAI
queue name ({"tenant:<id>": "name", "tenant:<id>:project:<slug>":
"name"}) consumed by services/k8s_kai_queues.py. Mapped foreign queues
are validated read-only and NEVER mutated — the map only redirects MOD's
dispatch; it does not own the queues. NULL = deterministic MOD queues
(``mod-t<tenant_id>``, created by MOD with its ownership label).

Additive + idempotent: ALTER TABLE ADD COLUMN IF NOT EXISTS — a second
run is a no-op.

Dry run (default, no --commit) PRINTS the SQL and exits 0 without
touching the database. The lead applies with --commit.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261003b_k8s_kai_queue_map.py            # dry run: prints SQL
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261003b_k8s_kai_queue_map.py --commit   # apply

LEAD APPLIES — not applied by the task agent.
"""
from __future__ import annotations

import argparse
import os
import sys

ALTER_SQL = """
ALTER TABLE k8s_cluster_configs ADD COLUMN IF NOT EXISTS kai_queue_map JSONB;
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
    statements = [ALTER_SQL]
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
    print("k8s_cluster_configs.kai_queue_map ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
