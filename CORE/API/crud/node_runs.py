"""Query layer for the node-run preview / fan-out endpoints (Gate 2b migration
of routers/node_runs_domains/node_runs.py — openspec api-refactor).

ONLY db.query(...) lives here for this domain (services/node_runs_service.py
owns policy/orchestration on top). Tenant scoping note: these reads are keyed
off a `workflow_run_id`/`graph_node_id` that the SERVICE has already resolved
against a tenant-scoped, view-gated `WorkflowRun` (via the pre-existing
`crud.get_workflow_run_by_id(..., tenant_id=...)` in crud/node_run_cud.py,
left untouched by this migration) — so NodeRun/ApiTaskQueue rows here are
reached only through an already-authorized run id, matching the original
inline-query behavior exactly. Cog/ContentBlob lookups take an explicit
`tenant_id` and filter on it directly, same as the original code.
"""
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

import models


def get_node_runs_with_workflow_node(db: Session, *, workflow_run_id: int) -> List[models.NodeRun]:
    """All NodeRuns for a run, eager-loading the (to-one, joinedload-safe)
    `workflow_node` relationship. Used by the previews endpoint."""
    return (
        db.query(models.NodeRun)
        .options(joinedload(models.NodeRun.workflow_node))
        .filter(models.NodeRun.workflow_run_id == workflow_run_id)
        .all()
    )


def get_task_node_payloads_for_run(
    db: Session, *, workflow_run_id: int
) -> List[Tuple[Optional[str], dict]]:
    """(node_id, task_payload) pairs for every task in a run — the minimal
    projection needed to compute per-node fan-out width."""
    return (
        db.query(models.ApiTaskQueue.node_id, models.ApiTaskQueue.task_payload)
        .filter(models.ApiTaskQueue.workflow_run_id == workflow_run_id)
        .all()
    )


def get_tasks_for_node(
    db: Session,
    *,
    workflow_run_id: int,
    graph_node_id: str,
    order: str = "asc",
) -> List[models.ApiTaskQueue]:
    """Every task attempt for one node in a run.

    `order="asc"` (enqueue order, used by fan-out pairing / dedup-to-latest)
    or `order="desc"` (newest attempt first, used by the versions list).
    """
    query = db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.workflow_run_id == workflow_run_id,
        models.ApiTaskQueue.node_id == graph_node_id,
    )
    if order == "desc":
        query = query.order_by(models.ApiTaskQueue.id.desc())
    else:
        query = query.order_by(models.ApiTaskQueue.id.asc())
    return query.all()


def get_workflow_node_by_graph_id(
    db: Session, *, workflow_id: int, graph_node_id: str
) -> Optional[models.WorkflowNode]:
    """Best-effort lookup of a workflow's node by its graph id (for display
    name resolution)."""
    return (
        db.query(models.WorkflowNode)
        .filter(
            models.WorkflowNode.workflow_id == workflow_id,
            models.WorkflowNode.graph_node_id == graph_node_id,
        )
        .first()
    )


def get_cog_for_tenant(db: Session, *, cog_id: int, tenant_id: int) -> Optional[models.Cog]:
    """A source cog, tenant-scoped (never cross-tenant)."""
    return (
        db.query(models.Cog)
        .filter(models.Cog.id == cog_id, models.Cog.tenant_id == tenant_id)
        .first()
    )


def get_content_blob_for_tenant(
    db: Session, *, content_id: str, tenant_id: int
) -> Optional[models.ContentBlob]:
    """A content blob (for its file_extension), tenant-scoped."""
    return (
        db.query(models.ContentBlob)
        .filter(models.ContentBlob.id == content_id, models.ContentBlob.tenant_id == tenant_id)
        .first()
    )
