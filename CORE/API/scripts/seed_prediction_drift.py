"""scheduler-drift-recal-scan GA.SDR.9 — inject synthetic prediction drift.

Inserts N COMPLETED ``api_task_queue`` rows claimed by ``--worker`` for
``--node-def-id``, each with a backfilled ``prediction_logs`` row whose
actual duration is ``predicted * skew``. The rows mirror the
``_seed`` helper in tests/test_scheduler_drift_scan.py column for column, so
``services.scheduler_drift_scan.aggregate_pair_mae`` picks them up as the
pair (node_definition_id, worker's hardware_profile_id).

Every seeded task payload carries ``"seeded_by": "seed_prediction_drift"``.
``--cleanup`` flips exactly those tasks (for the given node/worker) to
CANCELLED. Nothing is ever deleted (ISO 27001: no hard deletes). Cancelling
the task does NOT hide its prediction_logs row from the drift scan (the scan
does not filter on task status); the rows fall out of the scan once they age
past SCHEDULER_DRIFT_SCAN_WINDOW_H (default 24h).

Database selection: by default the live DB via Vault (``database.initialize``).
Set ``TEST_DATABASE_URL`` or ``TEST_DATABASE_NAME`` (same convention as
tests/conftest.py) to target a test DB directly with the static
POSTGRES_USER/POSTGRES_PASSWORD credentials instead.

    docker exec my_fastapi_app python scripts/seed_prediction_drift.py \\
        --node-def-id 42 --worker my-worker --n 20 --skew 2.0
    docker exec my_fastapi_app python scripts/seed_prediction_drift.py \\
        --node-def-id 42 --worker my-worker --cleanup
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SEED_TAG = "seed_prediction_drift"


def _select_database() -> str:
    """Point ``database.initialize`` at a test DB when asked (conftest convention).

    Must run BEFORE ``import database``: that module reads VAULT_ADDR at import
    time, and with VAULT_ADDR unset it falls back to static DATABASE_URL mode.
    Returns a label for the summary.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    name = os.environ.get("TEST_DATABASE_NAME")
    if not url and name:
        password = os.environ.get("POSTGRES_PASSWORD")
        if not password:
            raise SystemExit("TEST_DATABASE_NAME set but POSTGRES_PASSWORD is empty; "
                             "run inside my_fastapi_app or set TEST_DATABASE_URL.")
        user = os.environ.get("POSTGRES_USER", "modTech")
        host = os.environ.get("POSTGRES_HOST", "postgres-app")
        port = os.environ.get("POSTGRES_PORT", "5432")
        url = f"postgresql://{user}:{password}@{host}:{port}/{name}"
    if url:
        os.environ["DATABASE_URL"] = url
        os.environ.pop("VAULT_ADDR", None)
        return f"test DB {url.rsplit('/', 1)[-1]}"
    return f"live DB {os.environ.get('POSTGRES_DB', 'modTech')} (Vault)"


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Seed (or cancel) synthetic prediction drift for one "
                    "(node definition, worker) pair.")
    ap.add_argument("--node-def-id", type=int, required=True,
                    help="node_definitions.id to attribute the tasks to")
    ap.add_argument("--worker", required=True,
                    help="worker_licenses.worker_name that 'ran' the tasks")
    ap.add_argument("--n", type=int, default=20, help="rows to seed (default 20)")
    ap.add_argument("--skew", type=float, default=2.0,
                    help="actual = predicted * skew (default 2.0)")
    ap.add_argument("--pred-seconds", type=float, default=10.0,
                    help="predicted_duration_s per row (default 10.0)")
    ap.add_argument("--cleanup", action="store_true",
                    help="set previously seeded tasks to CANCELLED (never deletes)")
    args = ap.parse_args(argv)
    if args.n < 1:
        ap.error("--n must be >= 1")
    if args.pred_seconds <= 0:
        ap.error("--pred-seconds must be > 0")
    return args


def _seed(db, models, worker, node_def_id, n, pred, actual):
    """Mirror of tests/test_scheduler_drift_scan.py::_seed plus the seed tag."""
    done = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    task_ids, log_ids = [], []
    for _ in range(n):
        t = models.ApiTaskQueue(
            node_id="drift-workload", tenant_id=1, queue_name="CPU", priority=0,
            task_type="processing",
            task_payload={"node_definition_id": node_def_id, "seeded_by": SEED_TAG},
            status=models.ApiTaskStatus.COMPLETED, claimed_by_worker=worker,
            completed_at=done,
        )
        db.add(t)
        db.flush()
        pl = models.PredictionLog(
            task_id=t.id, model_version=1, inference_mode="cold",
            predicted_duration_s=pred, actual_duration_s=actual,
            completed_at=done,
        )
        db.add(pl)
        db.flush()
        task_ids.append(t.id)
        log_ids.append(pl.id)
    return task_ids, log_ids


_SEEDED_IDS_QUERY = """
    SELECT id FROM api_task_queue
    WHERE task_payload->>'seeded_by' = :tag
      AND task_payload->>'node_definition_id' = :node_definition_id
      AND claimed_by_worker = :worker
      AND status::text <> 'CANCELLED'
    ORDER BY id
"""


def _cleanup(db, models, worker, node_def_id):
    # Raw SQL for the JSONB filter: models.JSONB is a TypeDecorator, on which
    # the `.astext` comparator raises.
    from sqlalchemy import text
    ids = [r.id for r in db.execute(text(_SEEDED_IDS_QUERY), {
        "tag": SEED_TAG,
        "node_definition_id": str(int(node_def_id)),
        "worker": worker,
    }).fetchall()]
    if not ids:
        return []
    q = models.ApiTaskQueue
    rows = db.query(q).filter(q.id.in_(ids)).order_by(q.id.asc()).all()
    for r in rows:
        r.status = models.ApiTaskStatus.CANCELLED
    return [r.id for r in rows]


def _fmt_ids(ids):
    if not ids:
        return "(none)"
    if len(ids) > 10:
        return f"{ids[0]}..{ids[-1]} ({len(ids)} ids)"
    return ", ".join(str(i) for i in ids)


def main(argv=None) -> int:
    args = _parse_args(argv)
    target = _select_database()

    import database
    database.initialize()
    import models

    db = database.SessionLocal()
    try:
        if args.cleanup:
            ids = _cleanup(db, models, args.worker, args.node_def_id)
            db.commit()
            print(f"[{target}] cancelled {len(ids)} seeded task(s) for "
                  f"node_def={args.node_def_id} worker={args.worker}: {_fmt_ids(ids)}")
            print("  (no rows deleted; prediction_logs untouched and age out of the "
                  "drift window)")
            return 0

        worker = (db.query(models.WorkerLicense)
                  .filter(models.WorkerLicense.worker_name == args.worker).first())
        if worker is None:
            print(f"[{target}] worker '{args.worker}' not found in worker_licenses",
                  file=sys.stderr)
            return 2
        if worker.hardware_profile_id is None:
            print(f"[{target}] warning: worker '{args.worker}' has no hardware_profile_id; "
                  "the drift scan will skip this pair", file=sys.stderr)
        nd = (db.query(models.NodeDefinition.id)
              .filter(models.NodeDefinition.id == args.node_def_id)
              .execution_options(include_deleted=True).first())
        if nd is None:
            print(f"[{target}] node_definitions.id={args.node_def_id} not found",
                  file=sys.stderr)
            return 2

        pred = float(args.pred_seconds)
        actual = pred * float(args.skew)
        task_ids, log_ids = _seed(db, models, args.worker, args.node_def_id,
                                  args.n, pred, actual)
        db.commit()
        mae_pct = abs(actual - pred) / pred * 100.0
        print(f"[{target}] seeded {len(task_ids)} COMPLETED task(s) for "
              f"node_def={args.node_def_id} worker={args.worker} "
              f"hardware_profile={worker.hardware_profile_id}")
        print(f"  predicted={pred}s actual={actual}s (skew {args.skew}, MAE {mae_pct:.1f}%)")
        print(f"  api_task_queue ids: {_fmt_ids(task_ids)}")
        print(f"  prediction_logs ids: {_fmt_ids(log_ids)}")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
