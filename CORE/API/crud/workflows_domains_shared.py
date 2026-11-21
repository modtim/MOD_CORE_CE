"""Data-access layer for `routers/workflows_domains/_shared.py` (Gate 2b
migration of the SHARED sub-module itself — see `services/_base.py` for the
layering contract this follows, and `crud/node_definitions_shared.py` /
`services/node_definitions_shared_service.py` for the sibling package's
already-completed version of this exact migration).

`_shared.py` hosts no endpoint handlers of its own (Gate 2a's domain split
moved every `@router.*` handler into a per-domain sub-module); it is a pure
helpers/constants module that the sibling domain modules
`from routers.workflows_domains._shared import *`. This crud module is the
ONLY place `db.query(models.X)` lives for the handful of `_shared.py` helper
functions that embed inline queries — everything else in `_shared.py` (pure
policy/authz delegation, string/env logic, the node-metadata file scanner) has
no data access and is untouched.

Every fetch here is intentionally caller-UNSCOPED where the original inline
query was unscoped (no tenant filter baked in beyond what the original had)
— the service layer (`services/workflows_domains_shared_service.py`) applies
any additional policy on top, so a legitimate 403 never collapses into a 404
(SHARED RULES #5).
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


# ---------------------------------------------------------------------
# _user_can_see_timing
# ---------------------------------------------------------------------

def get_tenant(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    """Fetch a Tenant by id. Verbatim extraction — no tenant scoping to add
    (the Tenant row IS the tenant)."""
    return db.query(models.Tenant).filter(
        models.Tenant.id == tenant_id
    ).first()


# ---------------------------------------------------------------------
# _node_output_rows / _noderun_for — shared WorkflowNode/NodeRun lookups
# ---------------------------------------------------------------------

def get_workflow_node_by_graph_id(
    db: Session, workflow_id: int, graph_node_id: str,
) -> Optional[models.WorkflowNode]:
    """Fetch the WorkflowNode for a graph_node_id within a workflow. Verbatim
    extraction — used by both `_node_output_rows` and `_noderun_for`."""
    return db.query(models.WorkflowNode).filter(
        models.WorkflowNode.workflow_id == workflow_id,
        models.WorkflowNode.graph_node_id == graph_node_id,
    ).first()


def get_node_run_ids_for_workflow_node(
    db: Session, run_id: int, workflow_node_id: int,
) -> List[int]:
    """All NodeRun ids for one WorkflowNode within one run. Verbatim
    extraction from `_node_output_rows`."""
    return [r.id for r in db.query(models.NodeRun.id).filter(
        models.NodeRun.workflow_run_id == run_id,
        models.NodeRun.workflow_node_id == workflow_node_id,
    ).all()]


def get_workflow_node_outputs(
    db: Session, run_id: int, node_run_ids: List[int],
) -> List[models.WorkflowNodeOutput]:
    """WorkflowNodeOutput rows produced by any of `node_run_ids` in `run_id`.
    Verbatim extraction from `_node_output_rows`."""
    return db.query(models.WorkflowNodeOutput).filter(
        models.WorkflowNodeOutput.workflow_run_id == run_id,
        models.WorkflowNodeOutput.node_run_id.in_(node_run_ids),
    ).all()


def get_node_run_for_workflow_node(
    db: Session, run_id: int, workflow_node_id: int,
) -> Optional[models.NodeRun]:
    """The NodeRun for one WorkflowNode within one run. Verbatim extraction
    from `_noderun_for`."""
    return db.query(models.NodeRun).filter(
        models.NodeRun.workflow_run_id == run_id,
        models.NodeRun.workflow_node_id == workflow_node_id,
    ).first()


# ---------------------------------------------------------------------
# _create_imported_workflow / _validate_required_nodes
# ---------------------------------------------------------------------

def get_workflow_by_name(
    db: Session, tenant_id: int, name: str,
) -> Optional[models.Workflow]:
    """Active (non-deleted) Workflow with an exact name, scoped to the
    tenant. Verbatim extraction from `_create_imported_workflow`'s
    dedup-name loop — called repeatedly with an incrementing suffix until a
    free name is found, matching the original inline `while existing:`."""
    return db.query(models.Workflow).filter(
        models.Workflow.name == name,
        models.Workflow.tenant_id == tenant_id,
        models.Workflow.deleted_at.is_(None),
    ).first()


def get_node_definitions_for_tenant(
    db: Session, tenant_id: int,
) -> List[models.NodeDefinition]:
    """NodeDefinitions visible to a tenant: the tenant's own plus global
    (tenant_id IS NULL) definitions. Verbatim extraction — shared by
    `_create_imported_workflow` and `_validate_required_nodes`."""
    return db.query(models.NodeDefinition).filter(
        models.NodeDefinition.tenant_id.in_([tenant_id, None])
    ).all()
