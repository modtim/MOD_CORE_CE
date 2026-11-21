#!/usr/bin/env python3
"""455 / expr-1.4 (workflow-expressiveness-primitives) -- `subgraph_equivalence_index`:
one row per (workflow, subgraph) with its canonical hash, for the daily
analyzer that computes canonical hashes and equivalence-class membership.

revision      : 20261012j_subgraph_equivalence_index
down_revision : 20261012i_failure_prediction_tables

Purpose: the analyzer (consumer 3.5) writes one row per subgraph per workflow
run, recording the SHA-256 canonical hash and the set of node IDs. Consumer
3.6 then groups by (tenant_id, canonical_hash) to find same-tenant equivalent
subgraphs (banner when class_size >= 2). Consumer 3.7 does cross-tenant
visibility only when BOTH tenants opt in (code-gated, NOT a DB-level gate).

`canonical_hash` is VARCHAR(128) — the service (services/subgraph_canonical.py,
`canonical_hash()`) emits a sha256 hexdigest (64 chars), so 128 is roomy.

`hash_kind` allows 'byte' (the canonical SHA-256 of the normalised subgraph
AST, the default) and 'semantic' (reserved for future ML-based equivalence
signals). CHECK enforces both.

`node_ids` is JSONB: an array of node ID strings, the induced subgraph
membership. We avoid a JSONB unique index (Postgres does not support functional
unique indexes on JSONB values) and instead use `md5(node_ids::text)` which
works on the text representation of the JSON array.

Soft delete only (ISO 27001): `deleted_at` is a tombstone; the analyzer
soft-deletes stale rows rather than hard-removing them.

No hard delete anywhere. ON DELETE CASCADE for tenant and workflow_id (real
teardown -- a deleted workflow's equivalence-index rows must not survive to
poison equivalence-class counts for consumer 3.6). ON DELETE SET NULL (two
words) for workflow_version_id only, so the row survives a version delete.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012j_subgraph_equivalence_index.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "subgraph_equivalence_index"

_STATEMENTS = [
    (
        f"{_TABLE} table (expr-1.4, one row per subgraph per workflow run)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    workflow_id INTEGER NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    workflow_version_id INTEGER REFERENCES workflow_versions (id) ON DELETE SET NULL,
    canonical_hash VARCHAR(128) NOT NULL,
    hash_kind VARCHAR(16) NOT NULL DEFAULT 'byte'
        CHECK (hash_kind IN ('byte', 'semantic')),
    node_ids JSONB NOT NULL,
    node_count INTEGER NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
""",
    ),
    (
        f"{_TABLE} index (tenant_id, canonical_hash) WHERE deleted_at IS NULL",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_hash
    ON {_TABLE} (tenant_id, canonical_hash) WHERE deleted_at IS NULL;
""",
    ),
    (
        f"{_TABLE} index (canonical_hash) WHERE deleted_at IS NULL",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_cross_tenant
    ON {_TABLE} (canonical_hash) WHERE deleted_at IS NULL;
""",
    ),
    (
        f"{_TABLE} index (workflow_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_workflow
    ON {_TABLE} (workflow_id);
""",
    ),
    (
        f"{_TABLE} unique (workflow_id, hash_kind, canonical_hash, node_ids) WHERE deleted_at IS NULL",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live
    ON {_TABLE} (workflow_id, hash_kind, canonical_hash, md5(node_ids::text))
    WHERE deleted_at IS NULL;
""",
    ),
]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (getattr(raw, "driver_connection", None)
             or getattr(raw, "connection", None) or raw)
    dbapi.autocommit = True
    return dbapi


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply the CREATE TABLE + index steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (subgraph equivalence records are "
              "kept as historical analysis data; the table uses soft "
              "deletes only, so nothing is dropped on rollback)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        try:
            cur.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label}: {e}")
            return 1
        print(f"OK   {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    conn.close()
    print(
        "expr-1.4 ready: subgraph_equivalence_index exists; the analyzer "
        "(consumer 3.5) can write per-(workflow_id, subgraph) canonical "
        "hash rows for equivalence-class tracking."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
