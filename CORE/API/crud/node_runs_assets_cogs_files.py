"""CRUD layer for the `assets_cogs_files` node_runs sub-module
(Gate 2b migration of `routers/node_runs_domains/assets_cogs_files.py`
— see `services/_base.py` for the layering contract this follows).

The ONLY place `db.query(models.X)` lives for this slice of the
node_runs domain: the service-account "get all node runs for a
workflow run" endpoint (used by DAGs / export containers), which needs
the run's node runs plus their workflow-node/node-definition
relationship and the resolved storage paths of any output content
blobs.

Every fetch here is intentionally caller-UNSCOPED (no `tenant_id`
filter baked into the query) — the service layer
(`services/node_runs_assets_cogs_files_service.py`) applies the
`_enforce_user_run_view` tenant/ownership gate on top, so a legitimate
403 never collapses into a 404 (SHARED RULES #5).
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from sqlalchemy.orm import Session, joinedload

import models


def get_workflow_run(db: Session, run_id: int) -> Optional[models.WorkflowRun]:
    """Fetch one workflow run by id. Unscoped by design."""
    return db.query(models.WorkflowRun).filter(models.WorkflowRun.id == run_id).first()


def get_node_runs_with_node_definition(db: Session, run_id: int) -> List[models.NodeRun]:
    """All node runs for a workflow run, with `workflow_node` and its
    `node_definition` eager-loaded (needed for the `node_kind` property on
    `WorkflowNode`)."""
    return (
        db.query(models.NodeRun)
        .options(
            joinedload(models.NodeRun.workflow_node).joinedload(
                models.WorkflowNode.node_definition
            )
        )
        .filter(models.NodeRun.workflow_run_id == run_id)
        .all()
    )


def get_content_blobs_by_hashes(
    db: Session, content_hashes: Sequence[str]
) -> List[models.ContentBlob]:
    """Content blobs for a set of content hashes, used to resolve the
    storage path of node-run outputs."""
    if not content_hashes:
        return []
    return (
        db.query(models.ContentBlob)
        .filter(models.ContentBlob.id.in_(content_hashes))
        .all()
    )
