#!/usr/bin/env python3
"""456 / expr-1.3 (workflow-expressiveness-primitives) -- `workflows` gets
two columns: `extends_workflow_id` (FK -> workflows.id, the parent of an
extends chain) and `implements` (the interface name a workflow declares).

revision      : 20261012k_workflow_extends_implements
down_revision : 20261012j_subgraph_equivalence_index

`workflows` is an EXISTING table (models.py `class Workflow`) -- this is a
COLUMN add, not a table add. The lead applies this migration BEFORE any
models.py edit (bind-mount hazard); the lane does not touch models.py.

CHEAP ON A LARGE TABLE: both columns are added with no DEFAULT and no
CHECK inline, so ALTER TABLE ADD COLUMN does not rewrite the table --
Postgres appends a constant-NULL column in O(1). The CHECK is a separate
ALTER (validated per row on write, no bulk pass at ALTER time on a
NULL-valued column). Indexes are partial (WHERE ... IS NOT NULL) so they
stay tiny while the feature is opt-in.

Parent keying: services/workflow_inheritance.py is PURE -- resolve_extends
takes the parent GRAPH as data, it never looks the parent up. So the DB
key is our choice: we use the FK to workflows (id) (stable across renames;
the save path resolves the slug/name the UI shows into the id before
saving). ON DELETE SET NULL (two words): a child survives its parent's
hard delete -- extends_workflow_id clears, the child keeps running as its
own, self-contained workflow (ISO 27001: soft deletes everywhere; the
FK action only matters if a hard delete happens anyway).

`implements` is VARCHAR(64): services/workflow_interfaces.py validates
interface names against `^[a-z0-9][a-z0-9-]{0,63}$` -- max 64 chars, so
64 is exact, not roomy-by-luck.

CHECK ck_workflows_extends_not_self: a workflow may not extend ITSELF
(extends_workflow_id IS NULL OR extends_workflow_id <> id). Longer
extends cycles are caught by the save path (a cycle would also make the
overlay resolver loop); this CHECK kills the trivial 1-cycle at the DB.
Added via a DO $$ ... IF NOT EXISTS (pg_constraint) ... $$ block because
ALTER TABLE ADD CHECK has no IF NOT EXISTS form.

ix_workflows_extends_workflow_id answers "who extends me?" (child
enumeration, cascade previews). ix_workflows_tenant_implements answers
the 3.4 by-interface dispatch lookup: live workflows of a tenant
implementing interface X.

Idempotent: every statement is IF NOT EXISTS (the CHECK via the pg_constraint
guard); safe to re-run. Additive only: no drops, no ALTER on existing
columns. Downgrade is a no-op.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by
the lane -- the lead applies it.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20261012k_workflow_extends_implements.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "workflows"

_STATEMENTS = [
    (
        f"{_TABLE}.extends_workflow_id column (FK -> workflows.id, SET NULL)",
        f"""
ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS
    extends_workflow_id INTEGER REFERENCES {_TABLE} (id) ON DELETE SET NULL;
""",
    ),
    (
        f"{_TABLE}.implements column VARCHAR(64) (workflow_interfaces name)",
        f"""
ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS
    implements VARCHAR(64);
""",
    ),
    (
        f"{_TABLE} CHECK ck_workflows_extends_not_self",
        f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'ck_workflows_extends_not_self') THEN
        ALTER TABLE {_TABLE}
            ADD CONSTRAINT ck_workflows_extends_not_self
                CHECK (extends_workflow_id IS NULL OR extends_workflow_id <> id);
    END IF;
END
$$;
""",
    ),
    (
        f"{_TABLE} index ix_workflows_extends_workflow_id WHERE NOT NULL",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_extends_workflow_id
    ON {_TABLE} (extends_workflow_id) WHERE extends_workflow_id IS NOT NULL;
""",
    ),
    (
        f"{_TABLE} index ix_workflows_tenant_implements (tenant_id, implements)"
        " WHERE implements NOT NULL AND live [3.4 by-interface dispatch]",
        f"""
CREATE INDEX IF NOT EXISTS ix_{_TABLE}_tenant_implements
    ON {_TABLE} (tenant_id, implements)
    WHERE implements IS NOT NULL AND deleted_at IS NULL;
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
                   help="Apply the ALTER/DO/CREATE INDEX steps. Without "
                        "this flag, dry-run only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (the columns + index are additive on "
              "an existing table; rollback keeps them -- nothing is dropped)")
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
        "expr-1.3 ready: workflows.extends_workflow_id + workflows.implements "
        "exist; the save path can persist extends/implements and 3.4 can "
        "dispatch by interface."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
