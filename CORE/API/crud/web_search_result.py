"""CRUD layer for the WebSearch node result-return callback
(routers/web_search_result.py).

The ONLY place `db.query` for this router's endpoint lives (Gate 2b
service/CRUD migration). See `services/web_search_result_service.py` for the
policy layer built on top.

404-vs-403 (SHARED RULES #5): `get_workflow_run` stays tenant-UNSCOPED —
the service applies the run_task token's tenant/run scope via
`pin_run_task_tenant` AFTER the fetch, so an out-of-scope run 403s instead
of collapsing into a 404.
"""
from __future__ import annotations
from crud.node_run_cud import clamp_node_run_text

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_workflow_run(db: Session, workflow_run_id: int) -> Optional[models.WorkflowRun]:
    """Fetch a workflow run by id. Tenant-UNSCOPED on purpose (see module
    docstring) — the service enforces run_task token scope after the fetch."""
    return (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.id == workflow_run_id)
        .first()
    )


def get_latest_node_run(
    db: Session, workflow_run_id: int, graph_node_id: str
) -> Optional[models.NodeRun]:
    """Resolve the NodeRun for a graph node id within a run. Most-recent
    attempt wins (retries / re-dispatch create multiple NodeRuns for one
    graph node)."""
    return (
        db.query(models.NodeRun)
        .join(
            models.WorkflowNode,
            models.NodeRun.workflow_node_id == models.WorkflowNode.id,
        )
        .filter(
            models.NodeRun.workflow_run_id == workflow_run_id,
            models.WorkflowNode.graph_node_id == graph_node_id,
        )
        .order_by(models.NodeRun.id.desc())
        .first()
    )


def save_artifact(db: Session, node_run: models.NodeRun, artifact: dict) -> models.NodeRun:
    """Persist the artifact onto `NodeRun.outputs` (the same column the
    worker-task reporting path writes)."""
    node_run.outputs = clamp_node_run_text(artifact)
    db.commit()
    return node_run
