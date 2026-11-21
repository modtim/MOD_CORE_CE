"""Expand WorkflowTriggerType enum + add WorkflowRun.chain_depth.

Adds five new variants to the `workflowtriggertype` enum:
  - WORKFLOW_COMPLETION
  - WEBHOOK
  - FILE_EVENT
  - LOG_PATTERN_ALERT
  - QUEUE_EMPTY

Adds `workflow_runs.chain_depth INTEGER NOT NULL DEFAULT 0` so the
workflow-completion trigger can cap chain depth at 5 to prevent loops.

Adds two partial expression indexes for the hot firing paths:
  - idx_workflow_triggers_completion_source — completion-trigger lookup
    at run-status commit
  - idx_workflow_triggers_file_bucket — file-event lookup at MinIO
    event ingest

Idempotent: a second run is a no-op.

Run inside the API container:
    docker exec my_fastapi_app python3 migrations/20260426_expand_workflow_trigger_types.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


# Postgres stores Python enum members under their NAME (uppercase) when
# SQLAlchemy declares Column(Enum(WorkflowTriggerType)) without name override.
NEW_ENUM_VALUES = [
    "WORKFLOW_COMPLETION",
    "WEBHOOK",
    "FILE_EVENT",
    "LOG_PATTERN_ALERT",
    "QUEUE_EMPTY",
]


ALTER_ENUM_TEMPLATE = "ALTER TYPE workflowtriggertype ADD VALUE IF NOT EXISTS %s"

ADD_CHAIN_DEPTH_SQL = """
ALTER TABLE workflow_runs
    ADD COLUMN IF NOT EXISTS chain_depth INTEGER NOT NULL DEFAULT 0
"""

CREATE_COMPLETION_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_workflow_triggers_completion_source
ON workflow_triggers ((configuration->>'source_workflow_id'), tenant_id)
WHERE trigger_type = 'WORKFLOW_COMPLETION'
  AND deleted_at IS NULL
  AND is_active = true
"""

CREATE_FILE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_workflow_triggers_file_bucket
ON workflow_triggers ((configuration->>'bucket'))
WHERE trigger_type = 'FILE_EVENT'
  AND deleted_at IS NULL
  AND is_active = true
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


def _enum_values(cur) -> set[str]:
    cur.execute(
        """
        SELECT unnest(enum_range(NULL::workflowtriggertype))::text
        """
    )
    return {row[0] for row in cur.fetchall()}


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name=%s AND column_name=%s LIMIT 1
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def _index_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname=%s LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--commit",
        action="store_true",
        help="Apply the changes. Without this flag, dry-run only.",
    )
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    existing_enum = _enum_values(cur)
    enum_to_add = [v for v in NEW_ENUM_VALUES if v not in existing_enum]

    needs_chain_depth = not _column_exists(cur, "workflow_runs", "chain_depth")
    needs_completion_idx = not _index_exists(
        cur, "idx_workflow_triggers_completion_source"
    )
    needs_file_idx = not _index_exists(cur, "idx_workflow_triggers_file_bucket")

    if not (enum_to_add or needs_chain_depth or needs_completion_idx or needs_file_idx):
        print("All enum values, chain_depth column, and indexes already present — nothing to do.")
        return 0

    if enum_to_add:
        print(f"  enum values to add: {enum_to_add}")
    if needs_chain_depth:
        print("  workflow_runs.chain_depth column missing")
    if needs_completion_idx:
        print("  idx_workflow_triggers_completion_source missing")
    if needs_file_idx:
        print("  idx_workflow_triggers_file_bucket missing")

    if not args.commit:
        print("(dry run — re-run with --commit to apply)")
        return 0

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: ALTER TYPE needs the owner role (CE has no modTech)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for value in enum_to_add:
        print(f"  ALTER TYPE workflowtriggertype ADD VALUE {value!r}")
        cur.execute(ALTER_ENUM_TEMPLATE, (value,))
    if set_role_ok:
        cur.execute("RESET ROLE")

    if needs_chain_depth:
        print("  ALTER TABLE workflow_runs ADD COLUMN chain_depth ...")
        cur.execute(ADD_CHAIN_DEPTH_SQL)

    if needs_completion_idx:
        print("  CREATE INDEX idx_workflow_triggers_completion_source ...")
        cur.execute(CREATE_COMPLETION_INDEX_SQL)

    if needs_file_idx:
        print("  CREATE INDEX idx_workflow_triggers_file_bucket ...")
        cur.execute(CREATE_FILE_INDEX_SQL)

    final_enum = _enum_values(cur)
    missing = [v for v in NEW_ENUM_VALUES if v not in final_enum]
    if missing:
        print(f"ERROR: enum values still missing after ADD VALUE: {missing}", file=sys.stderr)
        return 1

    print("Applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
