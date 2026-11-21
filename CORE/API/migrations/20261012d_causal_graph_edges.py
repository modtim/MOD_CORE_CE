#!/usr/bin/env python3
"""wsip1.7 (workflow-self-improving-production decomp, §1.7) -- the ONE
new table: CAUSAL GRAPH EDGES (one row per causal edge of a workflow's
graph, structural or empirical).

revision      : 20261012d_causal_graph_edges
down_revision : 20261012c_add_mcp_tool_call_trigger_type

Purpose: sz-self-improving.1 (Phase-1 migrations) lands the storage this
tracker item needs; the WRITERS are pure services that will fill it later:
services/causal_graph.py writes the STRUCTURAL layer (edges derived from
graph_json, edge_kind='structural', node ids only) and services/causal_correlations.py
owns the EMPIRICAL layer (edge_kind='empirical', strength=<correlation
coefficient> -- see its docstring). Persisting those edges atomically is
tracker 5.3 (staging-then-swap), which is why every row carries a `generation`.

Columns, per the wsip1.7 decomp row:
    id                  BIGSERIAL PRIMARY KEY (never a max()-subquery
                        DEFAULT -- Postgres rejects subqueries in DEFAULT)
    tenant_id           INTEGER NOT NULL REFERENCES tenants (id)
                        ON DELETE CASCADE -- the graph belongs to the tenant
                        that owns the workflow; tenant deletion is a real
                        teardown.
    workflow_id         INTEGER NOT NULL REFERENCES workflows (id)
                        ON DELETE CASCADE -- the whole edge set is a
                        workflow's graph; when the workflow goes, its graph
                        goes with it (unlike production_drift_events, which
                        keeps detections as history via ON DELETE SET NULL).
    source_node_id      VARCHAR(255) NOT NULL -- node id as it appears in
                        workflows.graph_json (string, not a node_versions FK:
                        the causal graph addresses graph positions, not
                        pinned builds).
    target_node_id      VARCHAR(255) NOT NULL -- ditto.
    edge_kind           VARCHAR(16) NOT NULL with
                        CHECK (edge_kind IN ('structural','empirical')) --
                        the tracker's edge_kind enum. 'structural' = derived
                        from graph_json; 'empirical' = computed from sampled
                        per-node run lineage (services/causal_correlations.py).
    source_feature      VARCHAR(64) NULL -- per-node output feature name on
                        the source side. NULL means "the node as a whole"
                        (structural edges carry no feature granularity).
    target_feature      VARCHAR(64) NULL -- ditto, target side.
    strength            DOUBLE PRECISION NULL -- empirical edges: the
                        correlation coefficient (-1..1); structural edges
                        leave it NULL (no measured weight).
    sample_count        INTEGER NULL -- empirical edges: how many sampled
                        production runs the coefficient came from
                        (<= EMPIRICAL_MAX_SAMPLES); NULL on structural.
    generation          BIGINT NOT NULL DEFAULT 0 -- STAGING-THEN-SWAP
                        (tracker 5.3): a rebuild inserts a NEW generation
                        instead of rewriting the live set, readers pick
                        MAX(generation) per workflow, and the swap soft-deletes
                        the superseded generation. Default 0 so a plain
                        writer stays valid without knowing about generations.
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
    deleted_at          TIMESTAMPTZ NULL -- soft delete only (ISO 27001,
                        no hard delete); the staging-then-swap tombstones
                        superseded generations rather than dropping them.

Uniqueness -- DEVIATION from the tracker, explained:
    The tracker asks for a unique constraint on
    (workflow_id, source_node_id, target_node_id, edge_kind). That cannot
    hold as-is: staging-then-swap inserts a NEW generation for the same
    logical edge, and those rows would collide. So uniqueness is scoped to
    the generation AND to live rows:

        CREATE UNIQUE INDEX IF NOT EXISTS uq_causal_graph_edges_live
            ON causal_graph_edges
               (workflow_id, generation, source_node_id, target_node_id,
                edge_kind, COALESCE(source_feature, ''),
                COALESCE(target_feature, ''))
               WHERE deleted_at IS NULL;

    The COALESCE(...) legs collapse NULL ("whole node") to '' so two
    feature-less structural edges collide -- plain UNIQUE would treat the
    NULLs as distinct and let duplicates through (same trap as
    uq_plan_allowance_*). The WHERE clause keeps the constraint live-only so
    a soft-deleted generation never blocks its replacement.

Indexes:
    ix_causal_graph_edges_tenant on (tenant_id).
    ix_causal_graph_edges_workflow_generation on
        (workflow_id, generation DESC) -- the reader is "give me this
        workflow's newest generation", so generation sorts DESC.

Idempotent: CREATE TABLE / CREATE INDEX / CREATE UNIQUE INDEX IF NOT EXISTS;
safe to re-run. Additive only: no drops, no ALTER on existing tables.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012d_causal_graph_edges.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "causal_graph_edges"

_STATEMENTS = [
    (
        f"{_TABLE} table (wsip1.7, one row per causal graph edge)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    workflow_id INTEGER NOT NULL REFERENCES workflows (id) ON DELETE CASCADE,
    source_node_id VARCHAR(255) NOT NULL,
    target_node_id VARCHAR(255) NOT NULL,
    edge_kind VARCHAR(16) NOT NULL CHECK (edge_kind IN ('structural', 'empirical')),
    source_feature VARCHAR(64),
    target_feature VARCHAR(64),
    strength DOUBLE PRECISION,
    sample_count INTEGER,
    generation BIGINT NOT NULL DEFAULT 0,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
""",
    ),
    (
        f"{_TABLE} index (tenant_id)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant
    ON {_TABLE} (tenant_id);
""",
    ),
    (
        f"{_TABLE} index (workflow_id, generation DESC)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_workflow_generation
    ON {_TABLE} (workflow_id, generation DESC);
""",
    ),
    (
        f"{_TABLE} live unique (workflow_id, generation, source_node_id, "
        "target_node_id, edge_kind, COALESCE features)",
        f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_{_TABLE}_live
    ON {_TABLE} (workflow_id, generation, source_node_id, target_node_id,
        edge_kind, COALESCE(source_feature, ''), COALESCE(target_feature, ''))
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
                   help="Apply the CREATE TABLE + index + unique steps. "
                        "Without this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (causal edges are recomputable "
              "surveillance/graph data; the table is append-only with "
              "soft deletes only, so nothing is dropped on rollback)")
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
        "wsip1.7 ready: causal_graph_edges exists; services/causal_graph.py "
        "(structural) and services/causal_correlations.py (empirical) can "
        "write per-(workflow_id, generation) edge sets and readers pick "
        "max generation per workflow."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
