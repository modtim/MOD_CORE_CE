"""FX.40 part 2 (owner decision Q22=A) — scheduler-data aggregate roll-up.

Adds the tenant-free aggregate table the roll-up sweep folds aged rows into,
plus the soft-mark column on the source rows.

scheduler_training_aggregates (NEW, tenant-free by design):
    node_kind        VARCHAR(255) NOT NULL   task payload node_kind (fallback node_id)
    queue_name       VARCHAR(50)  NOT NULL   API task queue (CPU/CUDA/ROCm/Intel)
    gpu_model        VARCHAR(255) NULL       worker's HardwareProfile.gpu_model
    month            VARCHAR(7)   NOT NULL   "YYYY-MM" of task created_at
    succeeded        BOOLEAN      NOT NULL   status == completed
    task_count / duration_sum_s / duration_avg_s / duration_min_s /
    duration_max_s / duration_p50_s / duration_p95_s /
    peak_ram_max_mb / peak_ram_avg_mb / peak_vram_max_mb / peak_vram_avg_mb /
    peak_cpu_max_pct / peak_net_max_mbps

NO tenant_id, NO tenant_pseudonym, NO file names / prompts — only counts and
duration/peak stats, the exact columns the retrain reads (CORE/ML/dataset.py
targets: task_duration_seconds -> log_duration, per-task max ram/vram/cpu/net
-> peak_resources; node_kind/queue_name/gpu_model categoricals).

api_task_queue:
    aggregated_at    TIMESTAMPTZ NULL        when the sweep folded the row in

Why these columns: the ML retrain (CORE/ML/data_exporter.py + dataset.py)
joins TaskResourceSample -> ApiTaskQueue -> HardwareProfile. It reads, per
task: duration (completed-started), and the per-task MAX of ram_mb / vram_mb
/ cpu_percent / net_*_mbps, grouped by node_kind / queue / gpu_model. Rolled-up
months are read back through services/scheduler_pseudonymise.
read_aggregates_for_training, which emits those same columns.

Idempotent: CREATE TABLE IF NOT EXISTS + ADD COLUMN IF NOT EXISTS. Dry run
(default) PRINTS the SQL and exits 0 without touching the database. Apply
with --commit:
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007g_fx40_scheduler_aggregates.py            # dry run
    docker exec -w /app/CORE/API my_fastapi_app python3 \\
        migrations/20261007g_fx40_scheduler_aggregates.py --commit   # apply

models.py is bind-mounted live into the API container, so the table + column
must exist before any FX.40 part-2 code path touches them — apply immediately
after adding them to models.py.
"""
from __future__ import annotations

import argparse
import os
import sys

_STATEMENTS = [
    (
        "scheduler_training_aggregates table",
        """
CREATE TABLE IF NOT EXISTS scheduler_training_aggregates (
    id INTEGER PRIMARY KEY,
    node_kind VARCHAR(255) NOT NULL,
    queue_name VARCHAR(50) NOT NULL,
    gpu_model VARCHAR(255) NULL,
    month VARCHAR(7) NOT NULL,
    succeeded BOOLEAN NOT NULL DEFAULT false,
    task_count INTEGER NOT NULL DEFAULT 0,
    duration_sum_s DOUBLE PRECISION NOT NULL DEFAULT 0,
    duration_avg_s DOUBLE PRECISION NULL,
    duration_min_s DOUBLE PRECISION NULL,
    duration_max_s DOUBLE PRECISION NULL,
    duration_p50_s DOUBLE PRECISION NULL,
    duration_p95_s DOUBLE PRECISION NULL,
    peak_ram_max_mb DOUBLE PRECISION NULL,
    peak_ram_avg_mb DOUBLE PRECISION NULL,
    peak_vram_max_mb DOUBLE PRECISION NULL,
    peak_vram_avg_mb DOUBLE PRECISION NULL,
    peak_cpu_max_pct DOUBLE PRECISION NULL,
    peak_net_max_mbps DOUBLE PRECISION NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_scheduler_training_aggregates_bucket UNIQUE
        (node_kind, queue_name, gpu_model, month, succeeded)
);
""",
    ),
    (
        "scheduler_training_aggregates indexes",
        """
CREATE INDEX IF NOT EXISTS ix_scheduler_training_aggregates_node_kind
    ON scheduler_training_aggregates (node_kind);
CREATE INDEX IF NOT EXISTS ix_scheduler_training_agg_month
    ON scheduler_training_aggregates (month, node_kind);
""",
    ),
    (
        "api_task_queue.aggregated_at column",
        """
ALTER TABLE api_task_queue
    ADD COLUMN IF NOT EXISTS aggregated_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_api_task_queue_aggregated_at
    ON api_task_queue (aggregated_at);
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
        # one statement per semicolon — the batch may hold several
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        print(f"WRITE  {label}")
    if set_role_ok:
        cur.execute("RESET ROLE")
    cur.close()
    print("FX.40 scheduler aggregate roll-up schema ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
