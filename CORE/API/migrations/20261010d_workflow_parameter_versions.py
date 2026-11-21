#!/usr/bin/env python3
"""model + migration ONLY —
the ONE new table: WORKFLOW PARAMETER VERSIONS (per-training-step snapshot
of a differentiable workflow's parameters).

revision      : 20261010d_workflow_parameter_versions
down_revision : 20261010c_node_cost_benchmarks

The training loop integration that WRITES rows here (after each training
step completes + gradients are applied) is the orchestrator
half, in `services/workflow_orchestrator.py` — NOT part of this task and
NOT wired here. This migration only creates the storage; no writer/reader
code is added.

Columns:
    id                  BIGSERIAL PRIMARY KEY
    workflow_id         INTEGER NOT NULL REFERENCES workflows (id)
                        ON DELETE CASCADE — parameter version history has
                        no independent meaning once its owning workflow is
                        hard-deleted.
    version_id          UUID NOT NULL — assigned by the (not-yet-built)
                        training loop.
    parameters          JSONB NOT NULL — the full parameter snapshot.
    training_step_id    UUID — nullable; links back to the training step
                        that produced this snapshot.
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()

Index: ix_workflow_parameter_versions_workflow_created on
(workflow_id, created_at DESC) for "latest versions for this workflow"
reads (e.g. the future rollback endpoint).

`id` is BIGSERIAL PRIMARY KEY, never a max()-subquery DEFAULT.

Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS; safe to re-run.
Additive only: no drops, no ALTER on existing tables. The
`WorkflowParameterVersion` ORM class already exists in models.py (this
same task); this script only creates the backing table.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane — the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261010d_workflow_parameter_versions.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "workflow_parameter_versions"

_STATEMENTS = [
    (
        f"{_TABLE} table (sz-expr3.5, per-training-step parameter "
        "snapshot for differentiable workflows)",
        f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    workflow_id INTEGER NOT NULL REFERENCES workflows (id)
        ON DELETE CASCADE,
    version_id UUID NOT NULL,
    parameters JSONB NOT NULL,
    training_step_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""",
    ),
    (
        f"{_TABLE} index (workflow_id, created_at desc)",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_workflow_created
    ON {_TABLE} (workflow_id, created_at DESC);
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
        print("DRY  downgrade: no-op (parameter version rows are kept as "
              "historical training data; ON DELETE CASCADE on "
              "workflow_id handles cleanup when the owning workflow is "
              "hard-deleted)")
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
        "sz-expr3.5 ready: workflow_parameter_versions exists; the "
        "sz-expr3.4 training-loop writer and sz-expr3.6 rollback "
        "endpoint can now target it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
