"""CRUD layer for the `task_queue_scheduler` node_runs sub-module
(Gate 2b migration of `routers/node_runs_domains/task_queue_scheduler.py`
— see `services/_base.py` for the layering contract this follows).

The ONLY place `db.query(models.X)` lives for this slice of the
node_runs domain: per-workflow-run resource-usage rollups (used by
ReturnExport et al. to write `workflow_info.json`) and the two
graph-node-id-scoped node-run lookups used by the Traffic Cop DAG
service-account endpoints.

Every fetch here is intentionally caller-UNSCOPED (no `tenant_id` filter
baked into the query) — the service layer
(`services/node_runs_task_queue_service.py`) applies the
`_enforce_user_run_view` tenant/ownership gate on top, so a legitimate
403 never collapses into a 404 (SHARED RULES #5).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func as sql_func
from sqlalchemy.orm import Session, joinedload

import models


def get_workflow_run(db: Session, run_id: int) -> Optional[models.WorkflowRun]:
    """Fetch one workflow run by id. Unscoped by design."""
    return db.query(models.WorkflowRun).filter(models.WorkflowRun.id == run_id).first()


def get_pending_resource_aggregate_task_ids(db: Session, run_id: int) -> List[int]:
    """COMPLETED tasks for this run with no `task_resource_aggregate` row yet.

    The periodic aggregator runs hourly at :15; export containers fire right
    after the workflow completes, so this list materializes the aggregate
    on-demand instead of returning null integrals.
    """
    return [
        r[0]
        for r in db.query(models.ApiTaskQueue.id)
        .filter(
            models.ApiTaskQueue.workflow_run_id == run_id,
            models.ApiTaskQueue.status == models.ApiTaskStatus.COMPLETED,
            ~models.ApiTaskQueue.id.in_(db.query(models.TaskResourceAggregate.task_id)),
        )
        .all()
    ]


def get_node_run_resource_rows(db: Session, run_id: int) -> Sequence[Any]:
    """Per-node-run resource rows for a workflow run: NodeRun joined to
    WorkflowNode (for graph_node_id) and outer-joined to ApiTaskQueue +
    TaskResourceAggregate (task_id is api_task_queue.id;
    api_task_queue.node_run_id links back to node_runs)."""
    return (
        db.query(
            models.NodeRun.id.label("node_run_id"),
            models.NodeRun.workflow_node_id,
            models.NodeRun.status,
            models.NodeRun.started_at,
            models.NodeRun.completed_at,
            models.WorkflowNode.graph_node_id,
            models.ApiTaskQueue.id.label("task_id"),
            models.ApiTaskQueue.assigned_to_worker,
            models.TaskResourceAggregate.cpu_core_seconds,
            models.TaskResourceAggregate.ram_gb_seconds,
            models.TaskResourceAggregate.gpu_seconds,
            models.TaskResourceAggregate.vram_gb_seconds,
            models.TaskResourceAggregate.disk_read_mb,
            models.TaskResourceAggregate.disk_write_mb,
            models.TaskResourceAggregate.wall_clock_seconds,
        )
        .select_from(models.NodeRun)
        .join(
            models.WorkflowNode,
            models.WorkflowNode.id == models.NodeRun.workflow_node_id,
        )
        .outerjoin(
            models.ApiTaskQueue,
            models.ApiTaskQueue.node_run_id == models.NodeRun.id,
        )
        .outerjoin(
            models.TaskResourceAggregate,
            models.TaskResourceAggregate.task_id == models.ApiTaskQueue.id,
        )
        .filter(models.NodeRun.workflow_run_id == run_id)
        .order_by(models.NodeRun.id)
        .all()
    )


def get_resource_peaks_for_tasks(db: Session, task_ids: Sequence[int]) -> Sequence[Any]:
    """Bulk-load peak resource-sample stats for a batch of task ids (one
    round trip instead of one query per task)."""
    return (
        db.query(
            models.TaskResourceSample.task_id,
            sql_func.max(models.TaskResourceSample.cpu_percent).label("peak_cpu_percent"),
            sql_func.max(models.TaskResourceSample.ram_mb).label("peak_ram_mb"),
            sql_func.max(models.TaskResourceSample.vram_mb).label("peak_vram_mb"),
            sql_func.max(models.TaskResourceSample.gpu_util_pct).label("peak_gpu_util_pct"),
            sql_func.max(models.TaskResourceSample.gpu_temp_c).label("peak_gpu_temp_c"),
            sql_func.count(models.TaskResourceSample.id).label("sample_count"),
        )
        .filter(models.TaskResourceSample.task_id.in_(task_ids))
        .group_by(models.TaskResourceSample.task_id)
        .all()
    )


def get_node_kinds_for_workflow_nodes(db: Session, workflow_node_ids: Sequence[int]) -> Dict[int, Any]:
    """`node_kind` lives on NodeDefinition; resolve it via
    WorkflowNode.node_definition in one bulk query (avoids an N+1)."""
    rows = (
        db.query(models.WorkflowNode.id, models.NodeDefinition.node_kind)
        .join(
            models.NodeDefinition,
            models.NodeDefinition.id == models.WorkflowNode.node_definition_id,
        )
        .filter(models.WorkflowNode.id.in_(workflow_node_ids))
        .all()
    )
    return {wn_id: kind for wn_id, kind in rows}


def get_workflow_node_by_graph_id(
    db: Session, workflow_id: int, graph_node_id: str
) -> Optional[models.WorkflowNode]:
    """Fetch a WorkflowNode by its parent workflow + graph_node_id. Unscoped
    by design (tenant policy is enforced on the parent WorkflowRun)."""
    return (
        db.query(models.WorkflowNode)
        .filter(
            models.WorkflowNode.workflow_id == workflow_id,
            models.WorkflowNode.graph_node_id == graph_node_id,
        )
        .first()
    )


def get_node_run_for_workflow_node(
    db: Session,
    workflow_run_id: int,
    workflow_node_id: int,
    *,
    with_workflow_node: bool = False,
) -> Optional[models.NodeRun]:
    """Fetch the NodeRun for a given (workflow_run_id, workflow_node_id) pair.

    `with_workflow_node=True` eager-loads the `workflow_node` relationship
    (the GET-by-node-id endpoint returns it on the response model); the
    PATCH endpoint doesn't need it and matches the pre-migration plain query.
    """
    query = db.query(models.NodeRun)
    if with_workflow_node:
        query = query.options(joinedload(models.NodeRun.workflow_node))
    return query.filter(
        models.NodeRun.workflow_run_id == workflow_run_id,
        models.NodeRun.workflow_node_id == workflow_node_id,
    ).first()
