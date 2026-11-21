"""node-version-pinning — immutable node versions + per-workflow digest pins.

Creates `node_versions` (one append-only row per node build, keyed by image
digest) and adds:
  - workflow_nodes.pinned_node_version_id  → the build a workflow node runs
  - node_runs.executed_image_digest         → the build a run actually executed
  - node_runs.node_version_id               → link to that build

Backfill (idempotent):
  1. One `node_versions` row per node_definition that has an image_digest,
     capturing its CURRENT version/digest/variant map.
  2. Pin every existing workflow_node to the version matching its node's
     current build — so nothing changes under existing workflows; they are
     pinned to exactly what they would run today.

Nodes with NO image_digest (legacy tag-only system nodes) get no version and
their workflow_nodes stay NULL → the enqueue/worker path resolves them by tag
exactly as before. This migration is therefore behaviour-preserving on its own;
pinning only takes effect once the enqueue path starts sending digests.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260901_add_node_version_pinning.py --commit
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
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    )
    return cur.fetchone() is not None


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
        """,
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

    # 1. node_versions table
    if not _table_exists(cur, "node_versions"):
        steps.append((
            "create node_versions",
            """
            CREATE TABLE node_versions (
                id                  SERIAL PRIMARY KEY,
                node_definition_id  INTEGER NOT NULL REFERENCES node_definitions(id) ON DELETE CASCADE,
                node_kind           VARCHAR(250),
                version             INTEGER NOT NULL,
                image_digest        VARCHAR(100) NOT NULL,
                gpu_variants        JSONB,
                gpu_variant_digests JSONB,
                executable_path     VARCHAR(255),
                manifest_ref        VARCHAR(500),
                created_at          TIMESTAMPTZ DEFAULT now(),
                CONSTRAINT uq_node_version_digest UNIQUE (node_definition_id, image_digest)
            )
            """,
        ))
        steps.append((
            "index node_versions.node_definition_id",
            "CREATE INDEX IF NOT EXISTS ix_node_versions_node_definition_id "
            "ON node_versions(node_definition_id)",
        ))
        steps.append((
            "index node_versions.node_kind",
            "CREATE INDEX IF NOT EXISTS ix_node_versions_node_kind "
            "ON node_versions(node_kind)",
        ))

    # 2. workflow_nodes.pinned_node_version_id
    if not _column_exists(cur, "workflow_nodes", "pinned_node_version_id"):
        steps.append((
            "add workflow_nodes.pinned_node_version_id",
            "ALTER TABLE workflow_nodes "
            "ADD COLUMN pinned_node_version_id INTEGER REFERENCES node_versions(id)",
        ))
        steps.append((
            "index workflow_nodes.pinned_node_version_id",
            "CREATE INDEX IF NOT EXISTS ix_workflow_nodes_pinned_node_version_id "
            "ON workflow_nodes(pinned_node_version_id)",
        ))

    # 3. node_runs executed-digest columns
    if not _column_exists(cur, "node_runs", "executed_image_digest"):
        steps.append((
            "add node_runs.executed_image_digest",
            "ALTER TABLE node_runs ADD COLUMN executed_image_digest VARCHAR(100)",
        ))
    if not _column_exists(cur, "node_runs", "node_version_id"):
        steps.append((
            "add node_runs.node_version_id",
            "ALTER TABLE node_runs "
            "ADD COLUMN node_version_id INTEGER REFERENCES node_versions(id)",
        ))
        steps.append((
            "index node_runs.node_version_id",
            "CREATE INDEX IF NOT EXISTS ix_node_runs_node_version_id "
            "ON node_runs(node_version_id)",
        ))

    # --- Schema first ---
    if not steps:
        print("schema already current — checking backfill...")
    for label, stmt in steps:
        print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
        if args.commit:
            cur.execute(stmt)

    # 4. Backfill node_versions from current node_definitions (idempotent).
    # Digest is identity; each node gets exactly one backfilled row → version 1.
    backfill_versions = """
        INSERT INTO node_versions
            (node_definition_id, node_kind, version, image_digest, gpu_variants, executable_path, manifest_ref)
        SELECT nd.id, nd.node_kind, 1, nd.image_digest, nd.gpu_variants, nd.executable_path, nd.manifest_ref
          FROM node_definitions nd
         WHERE nd.image_digest IS NOT NULL AND nd.image_digest <> ''
           AND NOT EXISTS (
                SELECT 1 FROM node_versions nv
                 WHERE nv.node_definition_id = nd.id AND nv.image_digest = nd.image_digest
           )
    """
    # 5. Pin existing workflow_nodes to their node's current build (by digest).
    backfill_pins = """
        UPDATE workflow_nodes wn
           SET pinned_node_version_id = nv.id
          FROM node_definitions nd
          JOIN node_versions nv
            ON nv.node_definition_id = nd.id AND nv.image_digest = nd.image_digest
         WHERE wn.node_definition_id = nd.id
           AND wn.pinned_node_version_id IS NULL
    """

    # Only run backfill once the columns/table actually exist (they will after
    # --commit above, or already did).
    can_backfill = args.commit or (
        _table_exists(cur, "node_versions")
        and _column_exists(cur, "workflow_nodes", "pinned_node_version_id")
    )
    if can_backfill:
        for label, stmt in (
            ("backfill node_versions from current node_definitions", backfill_versions),
            ("pin existing workflow_nodes to current version", backfill_pins),
        ):
            print(f"{'WRITE' if args.commit else 'DRY  '}  {label}")
            if args.commit:
                cur.execute(stmt)
                print(f"        rows affected: {cur.rowcount}")
    else:
        print("DRY   (backfill skipped in dry-run until schema exists)")

    print("Applied." if args.commit else "(dry run — re-run with --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
