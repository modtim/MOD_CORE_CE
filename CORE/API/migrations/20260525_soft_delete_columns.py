"""Wizard-rebuild §11b — ISO 27001 soft-delete columns on 5 wizard tables.

Adds `deleted_at TIMESTAMPTZ` + `deleted_by_user_id INTEGER REFERENCES
users(id)` to the five tables that the wizard surfaces as user-deletable
content. The pre-existing SQLAlchemy `with_loader_criteria` hook in
`database.py` is extended in the same change to auto-filter these
models out of SELECTs (matches the existing Tenant / User pattern).

Tables touched:
  - node_definitions      (user-published nodes from the wizard)
  - build_specs           (per-node build artifacts)
  - workflow_runs         (per-run history; deletion = "hide this run")
  - node_variant_verification   (per-variant test rows; cascade with node)
  - repo_docs_extract     (Git-mode docs cache; tenant-scoped cleanup)

Also adds a CHECK constraint on `tenants.retention_days` so the value
stays in the ISO-permitted range [30, 2555] days (matches the §11b
spec: "tenant-configurable, bounded 30-2555 days per ISO 27001
A.8.2.3 + 18.1.3"). The existing default of 90 days is unchanged.

Partial indexes (`WHERE deleted_at IS NULL`) are added on the columns
most queried (node_definitions and workflow_runs are the hot paths;
the others see lower traffic). The loader-criteria hook makes most
selects unconditional `deleted_at IS NULL`, so a partial index is
the right shape.

Idempotent: every step is guarded by `information_schema` checks.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260525_soft_delete_columns.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


SOFT_DELETE_TABLES = (
    "node_definitions",
    "build_specs",
    "workflow_runs",
    "node_variant_verification",
    "repo_docs_extract",
)

# Tables we want a partial index on (`WHERE deleted_at IS NULL`). The
# selectivity of "row is live" is ~100% in steady state, so the partial
# index is tiny + fast on the hot SELECTs the loader-criteria filter
# auto-injects.
HOT_PATH_TABLES = SOFT_DELETE_TABLES


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
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def _constraint_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_constraint WHERE conname = %s",
        (name,),
    )
    return cur.fetchone() is not None


def _index_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = %s",
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

    for table in SOFT_DELETE_TABLES:
        if not _column_exists(cur, table, "deleted_at"):
            steps.append((
                f"add {table}.deleted_at",
                f"ALTER TABLE {table} "
                f"ADD COLUMN deleted_at TIMESTAMPTZ NULL",
            ))
        if not _column_exists(cur, table, "deleted_by_user_id"):
            steps.append((
                f"add {table}.deleted_by_user_id",
                f"ALTER TABLE {table} "
                f"ADD COLUMN deleted_by_user_id INTEGER NULL "
                f"REFERENCES users(id) ON DELETE SET NULL",
            ))

    for table in HOT_PATH_TABLES:
        ix_name = f"ix_{table}_live"
        if not _index_exists(cur, ix_name):
            # Partial index — only the live rows are indexed, so the
            # default SELECT (with auto-injected `deleted_at IS NULL`)
            # hits a tiny B-tree even on tables with millions of
            # tombstoned rows after years of churn.
            steps.append((
                f"partial index {ix_name}",
                f"CREATE INDEX {ix_name} ON {table} (id) "
                f"WHERE deleted_at IS NULL",
            ))

    # Bound tenants.retention_days to ISO-permitted range. Spec §11b:
    # "tenant-configurable 30-2555d". The column already exists with
    # default=90; we only add the CHECK if it's not already present.
    if not _constraint_exists(cur, "ck_tenants_retention_days_range"):
        steps.append((
            "tenants.retention_days CHECK (30..2555)",
            "ALTER TABLE tenants "
            "ADD CONSTRAINT ck_tenants_retention_days_range "
            "CHECK (retention_days BETWEEN 30 AND 2555)",
        ))

    # NodeDefinition unique-name handling under soft-delete. The
    # existing UNIQUE CONSTRAINT uq_node_def_tenant_user_name doesn't
    # know about deleted_at, so a soft-deleted "MyNode" would block a
    # user from creating a fresh "MyNode" until retention expires.
    # Replace with a partial unique index that excludes tombstones.
    # Same treatment for the system-node partial unique (tenant_id NULL).
    if _constraint_exists(cur, "uq_node_def_tenant_user_name"):
        steps.append((
            "drop legacy uq_node_def_tenant_user_name CONSTRAINT",
            "ALTER TABLE node_definitions "
            "DROP CONSTRAINT uq_node_def_tenant_user_name",
        ))
    if not _index_exists(cur, "uq_node_def_live_tenant_user_name"):
        steps.append((
            "partial unique uq_node_def_live_tenant_user_name",
            "CREATE UNIQUE INDEX uq_node_def_live_tenant_user_name "
            "ON node_definitions (tenant_id, user_id, name) "
            "WHERE deleted_at IS NULL",
        ))
    if _index_exists(cur, "uq_system_node_def_name"):
        steps.append((
            "drop legacy uq_system_node_def_name index",
            "DROP INDEX uq_system_node_def_name",
        ))
    if not _index_exists(cur, "uq_system_live_node_def_name"):
        steps.append((
            "partial unique uq_system_live_node_def_name",
            "CREATE UNIQUE INDEX uq_system_live_node_def_name "
            "ON node_definitions (name) "
            "WHERE tenant_id IS NULL AND deleted_at IS NULL",
        ))

    # RepoDocsExtract has UNIQUE(tenant_id, repo_url, ref,
    # extractor_version). Same soft-delete reuse concern: a stale
    # tombstoned cache row would prevent a fresh re-extract for the
    # same (tenant, repo, ref, version) tuple. Make it partial too.
    if _constraint_exists(cur, "uq_repo_docs_extract_key"):
        steps.append((
            "drop legacy uq_repo_docs_extract_key CONSTRAINT",
            "ALTER TABLE repo_docs_extract "
            "DROP CONSTRAINT uq_repo_docs_extract_key",
        ))
    if not _index_exists(cur, "uq_repo_docs_extract_live_key"):
        steps.append((
            "partial unique uq_repo_docs_extract_live_key",
            "CREATE UNIQUE INDEX uq_repo_docs_extract_live_key "
            "ON repo_docs_extract "
            "(tenant_id, repo_url, ref, extractor_version) "
            "WHERE deleted_at IS NULL",
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
