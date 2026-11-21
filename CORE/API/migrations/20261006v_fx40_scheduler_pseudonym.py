"""FX.40 part 1 (owner decision Q22=A) — scheduler-data pseudonymisation.

Adds the pseudonym columns to the ONE table the scheduler model actually
trains on:

api_task_queue:
    tenant_pseudonym    VARCHAR(40)  NULL  HMAC-SHA256(Vault key, tenant_id)
                                       hex truncated to 32 chars, "p_" prefix
    pseudonymised_at    TIMESTAMPTZ  NULL  when the sweep stamped the row

Why api_task_queue: the ML Retrain node (CORE/Metadata/system/ML_
Retrain_PROCESS_Node.py) queries prediction_logs + task_resource_samples,
and CORE/ML/data_exporter.py::export_training_data joins
TaskResourceSample -> ApiTaskQueue -> HardwareProfile -> NodeDefinition.
prediction_logs and task_resource_samples carry no tenant_id of their own
— they reach the tenant only through their task_id FK into api_task_queue,
whose tenant_id is the single tenant attribution on the training path
(node_definitions.tenant_id is node ownership, NULL = system node, not run
data). So pseudonymising api_task_queue pseudonymises the whole corpus.

`api_task_queue.tenant_id` stays nullable=False: authorization filtering
reads it everywhere, so nulling it is part 2 (nullability change + auth-
path rework) — reported as a blocker, NOT done here. No rows are deleted
(ISO 27001 — no hard deletes anywhere).

Idempotent: ADD COLUMN IF NOT EXISTS. Dry run (default) PRINTS the SQL and
exits 0 without touching the database. Apply with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261006v_fx40_scheduler_pseudonym.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261006v_fx40_scheduler_pseudonym.py --commit   # apply

models.py is bind-mounted live into the API container, so the columns must
exist before any FX.40 code path touches them — apply immediately after
adding them to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "api_task_queue.tenant_pseudonym column",
        """
ALTER TABLE api_task_queue
    ADD COLUMN IF NOT EXISTS tenant_pseudonym VARCHAR(40);
""",
    ),
    (
        "api_task_queue.pseudonymised_at column",
        """
ALTER TABLE api_task_queue
    ADD COLUMN IF NOT EXISTS pseudonymised_at TIMESTAMPTZ;
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
        for label, sql in _STATEMENTS:
            print(f"DRY  would: {label}")
            print(sql.rstrip())
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _owner_role  # CE-MIGROLE-1: CE has no modTech role — resolve the owner at runtime
    set_role_ok = _owner_role.maybe_set_role(cur)
    for label, sql in _STATEMENTS:
        cur.execute(sql)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.40 scheduler pseudonymisation columns ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
