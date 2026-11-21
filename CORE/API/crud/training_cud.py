"""Training-data-generation CRUD — the ONLY place `db.query(...)` lives for
the platform-admin "generate scheduler training data" domain
(`routers/training.py`).

These are platform-wide, cross-tenant admin aggregations (worker licenses,
resource samples/patterns, hardware profiles) plus a couple of
tenant-scoped lookups (workflow versions, cogs) used to pick inputs for the
synthetic dispatch loop. Access control (platform-admin gate) lives in the
router; every function below is a plain query/write primitive with no
policy.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

import models


# =============================================================================
# Workers
# =============================================================================


def licensed_active_workers(db: Session) -> List["models.WorkerLicense"]:
    """Licensed workers not paused and not mid-update-request.

    Platform-wide (workers aren't a single-tenant resource here); the
    "online within the last N minutes" heartbeat window is policy, applied
    by the service on this result.
    """
    return db.query(models.WorkerLicense).filter(
        models.WorkerLicense.status == models.WorkerStatus.LICENSED,
        models.WorkerLicense.paused == False,  # noqa: E712
        models.WorkerLicense.update_requested == False,  # noqa: E712
        models.WorkerLicense.deleted_at.is_(None),
    ).all()


# =============================================================================
# Workflow versions / workflows / cogs
# =============================================================================


def workflow_versions_by_id(db: Session, workflow_version_id: int) -> List["models.WorkflowVersion"]:
    """A specific workflow version, as a list (mirrors the original `.all()`
    shape so an empty result reads the same as "not found")."""
    return db.query(models.WorkflowVersion).filter(
        models.WorkflowVersion.id == workflow_version_id
    ).all()


def workflow_versions_with_graph_for_tenant(
    db: Session, tenant_id: int
) -> List["models.WorkflowVersion"]:
    """Every workflow version belonging to `tenant_id` that has a saved
    graph, for use as synthetic-dispatch candidates."""
    return db.query(models.WorkflowVersion).join(
        models.Workflow, models.WorkflowVersion.workflow_id == models.Workflow.id
    ).filter(
        models.Workflow.tenant_id == tenant_id,
        models.WorkflowVersion.graph_json.isnot(None),
    ).all()


def workflow_by_id(db: Session, workflow_id: int) -> Optional["models.Workflow"]:
    return db.query(models.Workflow).filter(models.Workflow.id == workflow_id).first()


def cogs_for_tenant(db: Session, tenant_id: int, *, limit: int) -> List["models.Cog"]:
    """Bounded set of a tenant's (non-deleted) cogs to use as dispatch
    inputs. `limit` is caller-supplied and always bounded (see service)."""
    return db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
    ).limit(limit).all()


def create_pending_run(
    db: Session,
    *,
    workflow_id: int,
    workflow_version_id: int,
    tenant_id: int,
    parameters: dict,
) -> "models.WorkflowRun":
    """Insert a not-yet-started `WorkflowRun` (status `QUEUED`) and flush
    (no commit — the service owns the transaction boundary across the whole
    dispatch loop).

    `TaskStatus` has no `PENDING` member (models.py:159-170: QUEUED / RUNNING /
    PENDING_ACTION / SUCCESS / FAILURE / COMPLETED / FAILED / BLOCKED /
    SUSPENDED / AWAITING_EXTERNAL_EVENT); `PENDING` belongs to the OTHER enum,
    `ApiTaskStatus` (models.py:8290), which is the worker task queue's status,
    not the run's. Naming the run's initial status after that enum made every
    dispatch iteration raise `AttributeError: PENDING` before the row was ever
    created, so the service's per-run handler logged "dispatch failed ... PENDING"
    and `dispatched` stayed 0. QUEUED is the canonical initial run status every
    other caller uses (workflow_orchestrator.py:3090 trigger path)."""
    run = models.WorkflowRun(
        workflow_id=workflow_id,
        workflow_version_id=workflow_version_id,
        tenant_id=tenant_id,
        status=models.TaskStatus.QUEUED,
        parameters=parameters,
    )
    db.add(run)
    db.flush()
    return run


# =============================================================================
# Training-data status counters
# =============================================================================


def count_resource_samples(db: Session) -> int:
    return db.query(models.TaskResourceSample).count()


def count_resource_patterns(db: Session) -> int:
    return db.query(models.ResourcePattern).count()


def count_completed_workflow_tasks(db: Session) -> int:
    return db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.status == models.ApiTaskStatus.COMPLETED,
        models.ApiTaskQueue.workflow_run_id.isnot(None),
    ).count()


def count_training_runs(db: Session) -> Tuple[int, int]:
    """`(dispatched, completed)` counts of runs tagged
    `training_data_generation` in `WorkflowRun.parameters`.

    `WorkflowRun.parameters` is a TypeDecorator wrapping JSON, so the
    PG-only `.astext` accessor isn't on the comparator — use the `->>`
    text-extract operator explicitly, which works whether the underlying
    type binds as JSON or JSONB.
    """
    training_filter = models.WorkflowRun.parameters.op("->>")("training_data_generation") == "true"
    total = db.query(models.WorkflowRun).filter(training_filter).count()
    completed = db.query(models.WorkflowRun).filter(
        training_filter,
        models.WorkflowRun.status == models.TaskStatus.COMPLETED,
    ).count()
    return total, completed


def count_unique_dag_types_with_patterns(db: Session) -> int:
    return db.query(models.ResourcePattern.node_kind).distinct().count()


def count_profiled_workers(db: Session) -> int:
    return db.query(models.HardwareProfile).filter(
        models.HardwareProfile.benchmark_status == "completed"
    ).count()
