#!/usr/bin/env python3
"""expr-10.1 -- `workflows` gets one column:
`differentiable` (bool, default false), flagging a workflow that opts in to
the differentiable-workflow save-time check (every node must declare a
`backward_function` or accept `numerical_gradient_wrap: true`; see brief
1341 for the numerical-gradient wrapper itself).

revision      : 20261013k_workflow_differentiable_column
down_revision : 20261013j_credit_usage_log_publish_invocation_unique

`workflows` is an EXISTING table (models.py `class Workflow`) -- this is a
COLUMN add, not a table add. The lead applies this migration BEFORE any
models.py edit (bind-mount hazard); the lane does not touch models.py.

NOT NULL DEFAULT false means existing rows get the default value written at
ALTER time -- a single pass over the table, but a boolean default is cheap
(no app-level backfill needed, unlike a FK or computed default).

Idempotent: ADD COLUMN IF NOT EXISTS; safe to re-run. Additive only: no
drops, no ALTER on existing columns. Downgrade is a no-op.

DRY-RUN by default; `--commit` applies. NEVER applied to the live DB by the
lane -- the lead applies it.

Run inside the API container:
    docker exec -w /app my_fastapi_app python3 \\
        migrations/20261013k_workflow_differentiable_column.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

_TABLE = "workflows"

_STATEMENTS = [
    (
        f"{_TABLE}.differentiable column BOOLEAN NOT NULL DEFAULT false",
        f"""
ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS
    differentiable BOOLEAN NOT NULL DEFAULT false;
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
                   help="Apply the ALTER step. Without this flag, dry-run "
                        "only.")
    args = p.parse_args()

    if not args.commit:
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        print("DRY  downgrade: no-op (the column is additive on an "
              "existing table; rollback keeps it -- nothing is dropped)")
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
        "expr-10.1 ready: workflows.differentiable exists; the save path "
        "can enforce the backward_function/numerical_gradient_wrap check "
        "for workflows that opt in."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
