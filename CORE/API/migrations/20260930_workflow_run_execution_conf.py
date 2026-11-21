"""GC.MWL.7b — manager-work-loans: durable execution-plan snapshot.

Adds one nullable JSONB column, `execution_conf`, to `workflow_runs`.

`routers/workflows_domains/workflows.py` (and every other run-creation path)
builds the run's execution plan (`nodes`/`edges`/`project_info`/`tenant_id`/
`tenant_bucket` — the `conf` dict `services.workflow_orchestrator.
start_workflow_execution` takes) once and hands it to the orchestrator
transiently. It is ALSO copied into `workflow_runs.parameters` today, but
that column is mutated throughout the run's life (execution_plan is written
into it once the orchestrator runs, plus cancelled_by / restart_history /
ad-hoc bookkeeping keys) and carries a live secret (`keycloak_token`) — it
cannot be trusted as a stable, secret-free plan to reload later.

`execution_conf` is a SEPARATE, write-once, secret-stripped copy
(`crud.workflow_cud._execution_conf_snapshot` drops `keycloak_token` /
`refresh_token` / other credential-shaped top-level keys before writing it)
captured at the same moment `parameters` is set, and never touched again.
It exists so a work-loan borrower manager (GC.MWL.7) can reload the plan and
dispatch a loaned run locally (`services/mesh_loans.py`
`_dispatch_loan_run`) — the gap the GC.MWL.7 seam's docstring called out as
the reason dispatch was a no-op.

Purely additive — no existing data touched (every existing row gets
execution_conf=NULL, which correctly means "no plan snapshot; a loan on
this run cannot be dispatched").

Idempotent: `ADD COLUMN IF NOT EXISTS`; safe to re-run.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260930_workflow_run_execution_conf.py --commit

Dry run (default, no --commit): prints the SQL it would run and exits 0
without touching the database.
"""
from __future__ import annotations

import argparse
import os
import sys


_TABLE = "workflow_runs"

ADD_EXECUTION_CONF_COLUMN_SQL = """
ALTER TABLE workflow_runs
    ADD COLUMN IF NOT EXISTS execution_conf JSONB;
"""

STATEMENTS = [
    ("add column workflow_runs.execution_conf", ADD_EXECUTION_CONF_COLUMN_SQL),
]


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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true")
    args = p.parse_args()

    if not args.commit:
        for label, sql in STATEMENTS:
            print(f"-- {label}")
            print(sql)
        print("(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    owner = _owner_role.owner_role(cur)
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        try:
            cur.execute(f'ALTER TABLE {_TABLE} OWNER TO {owner}')
        except Exception as e:  # noqa: BLE001
            print(f"(ALTER TABLE {_TABLE} OWNER TO {owner} failed: {e})")
        cur.execute("RESET ROLE")
    cur.close()
    print("workflow_runs.execution_conf ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
