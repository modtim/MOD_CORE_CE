"""Training-data-generation Service — business logic/policy for the
platform-admin "generate scheduler training data" endpoints
(`routers/training.py`).

Owns: the "online worker" heartbeat window, workflow-version/cog selection
policy, the synthetic-dispatch loop (round-robin across versions/cogs,
error-budget cutoff, ETA estimate) and the training-data readiness rollup.
All data access goes through `crud/training_cud.py` — no `db.query` here.

Auth (platform-admin gate) stays in the router — this module assumes the
caller has already been authorized.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy.orm import Session

import crud.training_cud as crud
from services._base import NotFoundError, ServiceError

# Canonical orchestrator handoff (same three-arg shape every other caller uses:
# CORE/API/routers/workflows_domains/workflows.py:2556 and
# CORE/API/services/workflow_orchestrator.py:3125-3130). Module-level so tests
# can monkeypatch `start_workflow_execution` / `threading.Thread` in THIS
# namespace. `database` carries no service imports, so this cannot cycle back
# here; other services already do the same top-level import
# (services/library_profiler.py:29, services/malware_scan_async.py:45).
import threading
from database import SessionLocal
from services.workflow_orchestrator import start_workflow_execution

logger = logging.getLogger(__name__)

#: Heartbeat recency window used to decide a licensed worker is "online".
ONLINE_HEARTBEAT_WINDOW = timedelta(minutes=2)

#: Max cogs fetched as dispatch-input candidates.
MAX_DISPATCH_COGS = 100

#: Dispatch loop bails after this many consecutive/total per-run errors.
MAX_DISPATCH_ERRORS = 10

#: Rough estimates used for the completion-time projection.
AVG_TASKS_PER_WORKFLOW = 3
TASKS_PER_HOUR_PER_WORKER = 120  # ~2 tasks/min/worker


class BadRequestError(ServiceError):
    """Malformed/unsatisfiable request (router maps -> 400)."""

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


def _online_workers(db: Session):
    """Licensed, active workers that have heartbeated within the online
    window."""
    workers = crud.licensed_active_workers(db)
    now = datetime.now(timezone.utc)
    return [
        w for w in workers
        if w.last_heartbeat and (now - w.last_heartbeat) < ONLINE_HEARTBEAT_WINDOW
    ]


def _resolve_versions(db: Session, *, tenant_id: int, workflow_version_id: Any):
    if workflow_version_id:
        versions = crud.workflow_versions_by_id(db, workflow_version_id)
        if not versions:
            raise NotFoundError("Workflow version not found")
    else:
        versions = crud.workflow_versions_with_graph_for_tenant(db, tenant_id)

    if not versions:
        raise BadRequestError(
            "No workflows found. Create at least one workflow with a saved version first."
        )
    return versions


def generate_training_data(db: Session, *, tenant_id: int, request: Any) -> Dict[str, Any]:
    """Dispatch synthetic workflow runs across online workers to collect
    scheduler resource-profiling data. Raises `BadRequestError` /
    `NotFoundError`; the router maps these to HTTP 400 / 404."""
    online_workers = _online_workers(db)
    if not online_workers:
        raise BadRequestError(
            "No online workers available. Add and license workers first."
        )

    versions = _resolve_versions(
        db, tenant_id=tenant_id, workflow_version_id=request.workflow_version_id
    )

    cogs = crud.cogs_for_tenant(db, tenant_id, limit=MAX_DISPATCH_COGS)
    if not cogs:
        raise BadRequestError(
            "No input files (cogs) found. Upload at least one file first."
        )

    dispatched = 0
    errors = 0
    workflow_names = set()
    total_target = request.runs_per_worker * len(online_workers)

    for i in range(total_target):
        version = versions[i % len(versions)]
        cog = cogs[i % len(cogs)]

        try:
            run = crud.create_pending_run(
                db,
                workflow_id=version.workflow_id,
                workflow_version_id=version.id,
                tenant_id=tenant_id,
                parameters={
                    "training_data_generation": True,
                    "run_index": i,
                },
            )

            # conf shape mirrors the canonical builders:
            # CORE/API/services/workflow_orchestrator.py:3067-3076 (trigger
            # path) and CORE/API/routers/workflows_domains/workflows.py:2043-
            # 2068 (Execute path); `workflow_run_id` stamped as at
            # workflow_orchestrator.py:3118 / workflows.py:2506.
            graph = version.graph_json or {}
            wf = crud.workflow_by_id(db, version.workflow_id)
            # Lazy, like the canonical trigger path
            # (workflow_orchestrator.py:2985/3065) — utils.minio_client pulls
            # services.platform_settings at its top, so keep it off this module's
            # import head.
            from utils.minio_client import get_tenant_bucket_name
            tenant_bucket = get_tenant_bucket_name(tenant_id, db)
            conf = {
                "tenant_id": tenant_id,
                "tenant_bucket": tenant_bucket,
                "nodes": graph.get("nodes", []),
                "edges": graph.get("edges", []),
                "project_info": {
                    "tenant_id": tenant_id,
                    "tenant_bucket": tenant_bucket,
                    "user_id": None,
                    "cog_ids": [cog.id],
                    "metadata_cog_ids": [],
                    "parent_folder_id": None,
                    "workflow_name": (wf.name if wf else "Workflow"),
                    "extra_variables": {},
                },
                "workflow_run_id": run.id,
            }

            # Same guard _start_workflow applies
            # (workflow_orchestrator.py:2198) — checked HERE so an empty graph
            # is a per-run error the request can report, not a 500 and not a
            # silently-dispatched run.
            if not conf["nodes"]:
                raise ValueError("No nodes in workflow conf")

            # The handoff thread opens a FRESH session (start_workflow_
            # execution -> db_factory(), workflow_orchestrator.py:2044), so the
            # pending run row must be committed before it starts.
            db.commit()

            threading.Thread(
                target=start_workflow_execution,
                args=(SessionLocal, conf, run.id),
                daemon=True,
            ).start()

            dispatched += 1
            if wf:
                workflow_names.add(wf.name)

        except Exception as e:
            logger.warning("Training data dispatch failed for run %d: %s", i, e)
            errors += 1
            if errors > MAX_DISPATCH_ERRORS:
                logger.error("Too many dispatch errors, stopping")
                break

    db.commit()

    total_tasks = dispatched * AVG_TASKS_PER_WORKFLOW
    tasks_per_hour = len(online_workers) * TASKS_PER_HOUR_PER_WORKER
    estimated_hours = round(total_tasks / max(tasks_per_hour, 1), 1)

    logger.info(
        "Training data generation: dispatched %d runs across %d workers, "
        "estimated %.1f hours",
        dispatched, len(online_workers), estimated_hours,
    )

    return {
        "dispatched": dispatched,
        "errors": errors,
        "workers": len(online_workers),
        "worker_names": [w.worker_name for w in online_workers],
        "workflows_used": list(workflow_names),
        "cogs_used": min(len(cogs), total_target),
        "estimated_hours": estimated_hours,
        "message": (
            f"Dispatched {dispatched} workflow runs across {len(online_workers)} workers. "
            f"Estimated completion: ~{estimated_hours} hours. "
            f"Resource samples will be collected automatically."
        ),
    }


def training_data_status(db: Session) -> Dict[str, Any]:
    """Readiness rollup for scheduler-model training data."""
    sample_count = crud.count_resource_samples(db)
    pattern_count = crud.count_resource_patterns(db)
    completed_tasks = crud.count_completed_workflow_tasks(db)
    training_runs, training_completed = crud.count_training_runs(db)
    unique_dags = crud.count_unique_dag_types_with_patterns(db)
    profiled_workers = crud.count_profiled_workers(db)

    ready = sample_count >= 5000 and unique_dags >= 5 and profiled_workers >= 5

    return {
        "resource_samples": sample_count,
        "resource_patterns": pattern_count,
        "completed_workflow_tasks": completed_tasks,
        "training_runs_dispatched": training_runs,
        "training_runs_completed": training_completed,
        "unique_dag_types_with_patterns": unique_dags,
        "profiled_workers": profiled_workers,
        "ready_for_training": ready,
        "recommendation": (
            "Ready to train!" if ready else
            f"Need more data: {max(0, 5000 - sample_count)} more samples, "
            f"{max(0, 5 - unique_dags)} more node categorys, "
            f"{max(0, 5 - profiled_workers)} more profiled workers."
        ),
    }
