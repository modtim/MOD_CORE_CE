"""Workflows-domain CRUD (Gate 2, stack-training-and-prewarm Workstream B).

The ONLY place `db.query(models.X)` (and other direct SQLAlchemy Core access)
lives for the endpoints migrated out of `routers/workflows_domains/workflows.py`.
Named `workflows_domain` (not `workflow_cud`) to avoid colliding with the
existing `crud/workflow_cud.py` module.

Pure data access: no auth/policy decisions, no response shaping. Callers
(the router, or `services/workflows_domain_service.py` for the endpoints with
real multi-step write policy) own everything else. Every function here is a
verbatim extraction of an inline query that used to live in the router —
filters/ordering are preserved exactly so behavior is unchanged.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Sequence, Tuple

from sqlalchemy import cast, String, update
from sqlalchemy.orm import Session

import models


# =============================================================================
# Tenant / billing (predict_workflow_cost)
# =============================================================================

def get_tenant(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


def get_subscription_for_tenant(db: Session, tenant_id: int) -> Optional[models.Subscription]:
    return db.query(models.Subscription).filter(
        models.Subscription.tenant_id == tenant_id
    ).first()


def get_plan(db: Session, plan_id: int) -> Optional[models.Plan]:
    return db.query(models.Plan).filter(models.Plan.id == plan_id).first()


# =============================================================================
# Workflow runs — task/node-run queries (cancel/pause/resume/node-controls/progress)
# =============================================================================

_ACTIVE_API_TASK_STATUSES = (
    models.ApiTaskStatus.PENDING,
    models.ApiTaskStatus.ASSIGNED,
    models.ApiTaskStatus.CLAIMED,
    models.ApiTaskStatus.RUNNING,
)


def conditional_cancel_run(db: Session, run_id: int, *, completed_at: datetime, error_message: str):
    """Atomically flip a QUEUED/RUNNING run to FAILED. Returns the CursorResult
    (caller checks `.rowcount` to detect a lost race)."""
    return db.execute(
        update(models.WorkflowRun)
        .where(
            models.WorkflowRun.id == run_id,
            models.WorkflowRun.status.in_([models.TaskStatus.QUEUED, models.TaskStatus.RUNNING]),
        )
        .values(
            status=models.TaskStatus.FAILED,
            completed_at=completed_at,
            error_message=error_message,
        )
    )


def get_active_tasks_for_run(db: Session, run_id: int) -> List[models.ApiTaskQueue]:
    return db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.workflow_run_id == run_id,
        models.ApiTaskQueue.status.in_(_ACTIVE_API_TASK_STATUSES),
    ).all()


def get_active_node_runs_for_run(db: Session, run_id: int) -> List[models.NodeRun]:
    return db.query(models.NodeRun).filter(
        models.NodeRun.workflow_run_id == run_id,
        models.NodeRun.status.in_([models.TaskStatus.QUEUED, models.TaskStatus.RUNNING]),
    ).all()


def get_completed_tasks_for_run(db: Session, run_id: int) -> List[models.ApiTaskQueue]:
    """Used by resume/resume-node to re-kick downstream dispatch."""
    return db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.workflow_run_id == run_id,
        models.ApiTaskQueue.status == models.ApiTaskStatus.COMPLETED,
    ).all()


def get_all_tasks_for_run_ordered(db: Session, run_id: int) -> List[models.ApiTaskQueue]:
    """Ascending by id — get_node_controls relies on "last write wins == newest"."""
    return (
        db.query(models.ApiTaskQueue)
        .filter(models.ApiTaskQueue.workflow_run_id == run_id)
        .order_by(models.ApiTaskQueue.id.asc())
        .all()
    )


def get_all_tasks_for_run(db: Session, run_id: int) -> List[models.ApiTaskQueue]:
    """Unordered — get_workflow_progress only aggregates, order doesn't matter."""
    return db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.workflow_run_id == run_id,
    ).all()


# =============================================================================
# Workflow row (schedule-pause / export)
# =============================================================================

def get_workflow_row(db: Session, workflow_id: int, tenant_id: int) -> Optional[models.Workflow]:
    """Plain tenant-scoped Workflow fetch, NO soft-delete filter (matches the
    original schedule-pause query exactly)."""
    return db.query(models.Workflow).filter(
        models.Workflow.id == workflow_id,
        models.Workflow.tenant_id == tenant_id,
    ).first()


def get_workflow_for_export(db: Session, workflow_id: int, tenant_id: int) -> Optional[models.Workflow]:
    """Tenant-scoped, excludes soft-deleted (matches the original export query)."""
    return db.query(models.Workflow).filter(
        models.Workflow.id == workflow_id,
        models.Workflow.tenant_id == tenant_id,
        models.Workflow.deleted_at.is_(None),
    ).first()


# =============================================================================
# Cogs (processing history / search / lineage / analytics)
# =============================================================================

def get_cogs_by_ids(db: Session, cog_ids: Sequence[int], tenant_id: int) -> List[models.Cog]:
    return db.query(models.Cog).filter(
        models.Cog.id.in_(cog_ids),
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
    ).all()


def get_cog(db: Session, cog_id: int, tenant_id: int) -> Optional[models.Cog]:
    return db.query(models.Cog).filter(
        models.Cog.id == cog_id,
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
    ).first()


def get_cog_children_files(db: Session, parent_id: int) -> List[models.Cog]:
    """FILE-type children of a cog (get_processing_history's `derived_assets`)."""
    return db.query(models.Cog).filter(
        models.Cog.parent_id == parent_id,
        models.Cog.cog_type == models.CogType.FILE,
        models.Cog.deleted_at.is_(None),
    ).all()


def search_cogs_by_processing(
    db: Session,
    tenant_id: int,
    *,
    min_cost: Optional[int] = None,
    max_cost: Optional[int] = None,
    processed_after: Optional[datetime] = None,
    processed_before: Optional[datetime] = None,
    workflow_name: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Tuple[List[models.Cog], int]:
    query = db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.cog_type == models.CogType.FILE,
        models.Cog.deleted_at.is_(None),
    )
    if min_cost is not None:
        query = query.filter(models.Cog.total_processing_cost >= min_cost)
    if max_cost is not None:
        query = query.filter(models.Cog.total_processing_cost <= max_cost)
    if processed_after:
        query = query.filter(models.Cog.last_processed_at >= processed_after)
    if processed_before:
        query = query.filter(models.Cog.last_processed_at <= processed_before)
    if workflow_name:
        query = query.filter(
            models.Cog.processing_metadata['workflow_runs'].astext.contains(workflow_name)
        )
    total = query.count()
    rows = query.limit(limit).offset(offset).all()
    return rows, total


def get_file_cogs_with_processing_metadata(db: Session, tenant_id: int, limit: int) -> List[models.Cog]:
    """FILE-type cogs with processing_metadata, capped at `limit` — used by
    get_quality_metrics_aggregation (no offset/total; matches the original
    `.limit(limit).all()` exactly)."""
    return (
        db.query(models.Cog)
        .filter(
            models.Cog.tenant_id == tenant_id,
            models.Cog.cog_type == models.CogType.FILE,
            models.Cog.processing_metadata.isnot(None),
            models.Cog.deleted_at.is_(None),
        )
        .limit(limit)
        .all()
    )


def get_cogs_with_processing_metadata(db: Session, tenant_id: int) -> List[models.Cog]:
    """Tenant-scoped cogs carrying processing_metadata (node-status/quality-metrics/
    resource-usage/cost-breakdown all scan this same shape)."""
    return db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.deleted_at.is_(None),
        models.Cog.processing_metadata.isnot(None),
    ).all()


def get_recently_processed_cogs(db: Session, tenant_id: int, cutoff_date: datetime) -> List[models.Cog]:
    return db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.cog_type == models.CogType.FILE,
        models.Cog.last_processed_at >= cutoff_date,
        models.Cog.deleted_at.is_(None),
    ).all()


def get_cog_parent(db: Session, parent_id: int) -> Optional[models.Cog]:
    """Single cog by id, no tenant filter (matches the original lineage-ancestor
    query, which is intentionally not re-scoped per hop)."""
    return db.query(models.Cog).filter(
        models.Cog.id == parent_id,
        models.Cog.deleted_at.is_(None),
    ).first()


def get_cog_children_all(db: Session, cog_id: int) -> List[models.Cog]:
    """All child cogs (any type) — lineage descendants."""
    return db.query(models.Cog).filter(
        models.Cog.parent_id == cog_id,
        models.Cog.deleted_at.is_(None),
    ).all()


def get_output_folder_cogs(db: Session, tenant_id: int, run_id: int) -> List[models.Cog]:
    return db.query(models.Cog).filter(
        models.Cog.tenant_id == tenant_id,
        models.Cog.cog_type == models.CogType.ASSET_FOLDER,
        cast(models.Cog.processing_metadata['workflow_run_id'], String) == str(run_id),
        models.Cog.deleted_at.is_(None),
    ).all()


# =============================================================================
# Node-run / task-queue joins (node-status, runs-grid, detailed-log)
# =============================================================================

def get_node_run_join_rows(db: Session, run_id: int):
    """(NodeRun, graph_node_id) pairs for one run — get_node_execution_status."""
    return (
        db.query(models.NodeRun, models.WorkflowNode.graph_node_id)
        .join(models.WorkflowNode, models.NodeRun.workflow_node_id == models.WorkflowNode.id)
        .filter(models.NodeRun.workflow_run_id == run_id)
        .all()
    )


def get_run_grid_rows(db: Session, run_ids: Sequence[int]):
    """(workflow_run_id, graph_node_id, node_name, status) rows across many runs —
    get_workflow_runs_grid. Empty when `run_ids` is empty (matches the original
    `if run_ids:` guard)."""
    if not run_ids:
        return []
    return (
        db.query(
            models.NodeRun.workflow_run_id,
            models.WorkflowNode.graph_node_id,
            models.NodeDefinition.name,
            models.NodeRun.status,
        )
        .join(models.WorkflowNode, models.NodeRun.workflow_node_id == models.WorkflowNode.id)
        .outerjoin(models.NodeDefinition, models.WorkflowNode.node_definition_id == models.NodeDefinition.id)
        .filter(models.NodeRun.workflow_run_id.in_(run_ids))
        .all()
    )


def get_node_runs_for_run(db: Session, run_id: int) -> List[models.NodeRun]:
    return db.query(models.NodeRun).filter(
        models.NodeRun.workflow_run_id == run_id
    ).all()


def get_tasks_by_node_run_ids(db: Session, node_run_ids: Sequence[int], run_id: int) -> List[models.ApiTaskQueue]:
    if not node_run_ids:
        return []
    return db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.node_run_id.in_(node_run_ids),
        models.ApiTaskQueue.workflow_run_id == run_id,
    ).all()


def get_run_events(db: Session, run_id: int) -> List[Any]:
    """Raw-SQL read of the orchestrator event log (workflow_run_events has no
    ORM model). Returns the raw row tuples; caller shapes them. Any DB error is
    the caller's concern (get_workflow_detailed_log treats this as best-effort)."""
    from sqlalchemy import text

    return db.execute(
        text(
            "SELECT ts, source, level, message, event_metadata "
            "FROM workflow_run_events "
            "WHERE workflow_run_id = :rid "
            "ORDER BY ts ASC, id ASC"
        ),
        {"rid": run_id},
    ).fetchall()
