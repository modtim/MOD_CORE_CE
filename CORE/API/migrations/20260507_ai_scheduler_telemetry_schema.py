"""AI scheduler telemetry schema — single migration for all new schema.

Bundled into one migration so the rolling deploy can't end up in a state
where some columns exist and others don't.

Adds:

  Tables (new):
    - worker_system_state         (1:1 with worker_licenses; kernel/OS/package telemetry,
                                   air_gapped flag, baseline snapshots taken at calibration)
    - prediction_training_marker  (singleton row; tracks recalibrations_since_last_train
                                   counter and last_train_at for the auto-retrain trigger)

  Columns (additions to existing tables):
    - accelerator_profiles.baseline_driver_version
    - hardware_profiles.calibration_status, last_calibrated_at
        (per-profile staleness, distinct from existing per-NodeDefinition calibration)
    - workflow_versions.priority_class, prewarm
    - workflow_runs.priority_class, estimated_duration_seconds, input_size_bytes
    - node_runs.estimated_duration_seconds, input_size_bytes
    - api_task_queue.priority_class

Defaults are chosen so existing rows behave exactly as today: every priority_class
defaults to 'normal' (current FIFO behaviour), prewarm defaults to false, calibration
fields nullable. Backwards-compat by construction.

Idempotent — re-running with --commit is a safe no-op.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260507_ai_scheduler_telemetry_schema.py --commit
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


def _index_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (name,))
    return cur.fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = _get_conn()
    cur = conn.cursor()

    steps: list[tuple[str, str]] = []

    # ------------------------------------------------------------------
    # 1. worker_system_state — kernel/OS/package telemetry per worker.
    # ------------------------------------------------------------------
    if not _table_exists(cur, "worker_system_state"):
        steps.append((
            "create worker_system_state table",
            """
            CREATE TABLE worker_system_state (
                id SERIAL PRIMARY KEY,
                worker_license_id INTEGER NOT NULL REFERENCES worker_licenses(id) ON DELETE CASCADE,
                kernel_version VARCHAR(128),
                os_distro VARCHAR(64),
                os_version VARCHAR(64),
                package_count INTEGER NOT NULL DEFAULT 0,
                air_gapped BOOLEAN NOT NULL DEFAULT FALSE,
                baseline_kernel_version VARCHAR(128),
                baseline_os_version VARCHAR(64),
                baseline_package_count INTEGER,
                last_baselined_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_worker_system_state_license UNIQUE (worker_license_id)
            )
            """,
        ))
        steps.append((
            "index worker_system_state.air_gapped (partial)",
            "CREATE INDEX IF NOT EXISTS ix_worker_system_state_air_gapped "
            "ON worker_system_state(worker_license_id) WHERE air_gapped = TRUE",
        ))

    # ------------------------------------------------------------------
    # 2. prediction_training_marker — singleton retrain-trigger state.
    # ------------------------------------------------------------------
    if not _table_exists(cur, "prediction_training_marker"):
        steps.append((
            "create prediction_training_marker table",
            """
            CREATE TABLE prediction_training_marker (
                id SERIAL PRIMARY KEY,
                last_train_at TIMESTAMPTZ,
                last_train_model_version INTEGER,
                recalibrations_since_last_train INTEGER NOT NULL DEFAULT 0,
                last_check_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT prediction_training_marker_singleton CHECK (id = 1)
            )
            """,
        ))
        steps.append((
            "seed prediction_training_marker singleton row",
            "INSERT INTO prediction_training_marker (id) VALUES (1) ON CONFLICT DO NOTHING",
        ))

    # ------------------------------------------------------------------
    # 3. accelerator_profiles.baseline_driver_version
    # ------------------------------------------------------------------
    if not _column_exists(cur, "accelerator_profiles", "baseline_driver_version"):
        steps.append((
            "add accelerator_profiles.baseline_driver_version",
            "ALTER TABLE accelerator_profiles "
            "ADD COLUMN baseline_driver_version VARCHAR(64)",
        ))

    # ------------------------------------------------------------------
    # 4. hardware_profiles per-profile calibration tracking.
    # ------------------------------------------------------------------
    if not _column_exists(cur, "hardware_profiles", "calibration_status"):
        steps.append((
            "add hardware_profiles.calibration_status",
            "ALTER TABLE hardware_profiles "
            "ADD COLUMN calibration_status VARCHAR(20) NOT NULL DEFAULT 'uncalibrated'",
        ))
        steps.append((
            "index hardware_profiles.calibration_status",
            "CREATE INDEX IF NOT EXISTS ix_hardware_profiles_calibration_status "
            "ON hardware_profiles(calibration_status)",
        ))
    if not _column_exists(cur, "hardware_profiles", "last_calibrated_at"):
        steps.append((
            "add hardware_profiles.last_calibrated_at",
            "ALTER TABLE hardware_profiles "
            "ADD COLUMN last_calibrated_at TIMESTAMPTZ",
        ))

    # ------------------------------------------------------------------
    # 5. workflow_versions priority + prewarm
    # ------------------------------------------------------------------
    if not _column_exists(cur, "workflow_versions", "priority_class"):
        steps.append((
            "add workflow_versions.priority_class",
            "ALTER TABLE workflow_versions "
            "ADD COLUMN priority_class VARCHAR(20) NOT NULL DEFAULT 'normal'",
        ))
    if not _column_exists(cur, "workflow_versions", "prewarm"):
        steps.append((
            "add workflow_versions.prewarm",
            "ALTER TABLE workflow_versions "
            "ADD COLUMN prewarm BOOLEAN NOT NULL DEFAULT FALSE",
        ))

    # ------------------------------------------------------------------
    # 6. workflow_runs priority_class + estimated_duration + input_size
    # ------------------------------------------------------------------
    if not _column_exists(cur, "workflow_runs", "priority_class"):
        steps.append((
            "add workflow_runs.priority_class",
            "ALTER TABLE workflow_runs "
            "ADD COLUMN priority_class VARCHAR(20) NOT NULL DEFAULT 'normal'",
        ))
        steps.append((
            "index workflow_runs.priority_class",
            "CREATE INDEX IF NOT EXISTS ix_workflow_runs_priority_class "
            "ON workflow_runs(priority_class)",
        ))
    if not _column_exists(cur, "workflow_runs", "estimated_duration_seconds"):
        steps.append((
            "add workflow_runs.estimated_duration_seconds",
            "ALTER TABLE workflow_runs "
            "ADD COLUMN estimated_duration_seconds INTEGER",
        ))
    if not _column_exists(cur, "workflow_runs", "input_size_bytes"):
        steps.append((
            "add workflow_runs.input_size_bytes",
            "ALTER TABLE workflow_runs "
            "ADD COLUMN input_size_bytes BIGINT",
        ))

    # ------------------------------------------------------------------
    # 7. node_runs estimated_duration + input_size
    # ------------------------------------------------------------------
    if not _column_exists(cur, "node_runs", "estimated_duration_seconds"):
        steps.append((
            "add node_runs.estimated_duration_seconds",
            "ALTER TABLE node_runs "
            "ADD COLUMN estimated_duration_seconds INTEGER",
        ))
    if not _column_exists(cur, "node_runs", "input_size_bytes"):
        steps.append((
            "add node_runs.input_size_bytes",
            "ALTER TABLE node_runs "
            "ADD COLUMN input_size_bytes BIGINT",
        ))

    # ------------------------------------------------------------------
    # 8. api_task_queue.priority_class
    # ------------------------------------------------------------------
    if not _column_exists(cur, "api_task_queue", "priority_class"):
        steps.append((
            "add api_task_queue.priority_class",
            "ALTER TABLE api_task_queue "
            "ADD COLUMN priority_class VARCHAR(20) NOT NULL DEFAULT 'normal'",
        ))
        steps.append((
            "index api_task_queue.priority_class",
            "CREATE INDEX IF NOT EXISTS ix_api_task_queue_priority_class "
            "ON api_task_queue(priority_class, status)",
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
