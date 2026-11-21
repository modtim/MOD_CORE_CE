"""P-wizard B — Workflow Templates.

Curated starter workflows users can clone into their workspace as a
new Workflow + WorkflowVersion. Three endpoints:

  GET  /workflow-templates                — list curated + tenant rows
  GET  /workflow-templates/{id}           — single template (with graph)
  POST /workflow-templates/{id}/clone     — copy into the user's workspace

Cloning validates that every node referenced by the template's graph is
actually visible to the cloning user (system + tenant + private). Any
missing node aborts the clone with 422 + the missing-name list, so the
user can build the prerequisite nodes in the Node Wizard first.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import crud
import models
import schemas
from dependencies import get_db, require_active_and_onboarded_user
from services.workflow_templates_service import (
    get_visible_template,
    list_visible_templates,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/workflow-templates", tags=["Workflow Templates"])


class TemplateSummary(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    plan_required: Optional[str] = None
    is_curated: bool
    preview_nodes: Optional[List[str]] = None


class TemplateDetail(TemplateSummary):
    graph_json: Dict[str, Any]


class TemplateClonePayload(BaseModel):
    new_name: Optional[str] = Field(default=None, max_length=255)


class TemplateCloneResponse(BaseModel):
    workflow_id: int
    version_id: int
    name: str


def _to_summary(t: models.WorkflowTemplate) -> TemplateSummary:
    preview = t.preview_nodes if isinstance(t.preview_nodes, list) else None
    return TemplateSummary(
        id=t.id,
        name=t.name,
        description=t.description,
        category=t.category,
        plan_required=t.plan_required,
        is_curated=t.is_curated,
        preview_nodes=preview,
    )


@router.get("", response_model=List[TemplateSummary])
def list_templates(
    category: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Return curated platform-wide templates + any private to this tenant.

    Curated rows always come first so the gallery's primary content is
    the library MOD ships, with tenant-private extensions tucked below.
    """
    rows = list_visible_templates(
        db, tenant_id=current_user.tenant_id, category=category,
    )
    return [_to_summary(t) for t in rows]


@router.get("/{template_id}", response_model=TemplateDetail)
def get_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    t = get_visible_template(
        db, template_id=template_id, tenant_id=current_user.tenant_id,
    )
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    summary = _to_summary(t).model_dump()
    summary["graph_json"] = t.graph_json or {"nodes": [], "edges": []}
    return TemplateDetail(**summary)


@router.post("/{template_id}/clone", response_model=TemplateCloneResponse,
             status_code=status.HTTP_201_CREATED)
def clone_template(
    template_id: int,
    payload: TemplateClonePayload,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Clone a template into a new Workflow + initial WorkflowVersion.

    Validates every node_definition_name referenced by the graph is
    visible to the cloning user. Missing nodes → 422 with the list, so
    the wizard panel can offer "Build these nodes first" actions.
    """
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="user has no tenant")

    t = get_visible_template(
        db, template_id=template_id, tenant_id=current_user.tenant_id,
    )
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")

    graph = t.graph_json or {"nodes": [], "edges": []}
    if not isinstance(graph, dict):
        raise HTTPException(status_code=500, detail="template graph is malformed")
    nodes = graph.get("nodes") or []

    # Collect referenced node names. Templates use names (not ids) so the
    # graph is portable across tenants — clone resolves names against
    # the cloning user's available NodeDefinitions.
    referenced_names = []
    for n in nodes:
        if isinstance(n, dict):
            data = n.get("data") or {}
            ref = data.get("nodeDefinitionName") or n.get("nodeDefinitionName")
            if ref:
                referenced_names.append(ref)

    missing: List[str] = []
    name_to_id: Dict[str, int] = {}
    for ref_name in set(referenced_names):
        nd = crud.get_node_definition_by_name(
            db, name=ref_name, tenant_id=current_user.tenant_id,
            user_id=current_user.id, include_system=True,
        )
        if nd is None:
            missing.append(ref_name)
        else:
            name_to_id[ref_name] = nd.id

    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "missing_nodes",
                "message": "Template references nodes that are not in your library yet.",
                "missing": sorted(missing),
            },
        )

    # Resolve every node graph entry to point at the user's NodeDefinition id.
    resolved_nodes = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        data = dict(n.get("data") or {})
        ref_name = data.pop("nodeDefinitionName", None) or n.get("nodeDefinitionName")
        if ref_name and ref_name in name_to_id:
            data["nodeDefinitionId"] = name_to_id[ref_name]
            data.setdefault("label", ref_name)
        new_node = dict(n)
        new_node["data"] = data
        new_node.pop("nodeDefinitionName", None)
        resolved_nodes.append(new_node)
    resolved_graph = dict(graph)
    resolved_graph["nodes"] = resolved_nodes

    new_name = (payload.new_name or t.name).strip() or t.name
    db_workflow = crud.create_workflow(
        db,
        workflow=schemas.WorkflowCreate(name=new_name),
        user_id=current_user.id,
        tenant_id=current_user.tenant_id,
    )
    db_version = crud.create_workflow_version(
        db,
        workflow=db_workflow,
        version_create=schemas.WorkflowVersionCreate(graph_json=resolved_graph),
        user_id=current_user.id,
    )
    return TemplateCloneResponse(
        workflow_id=db_workflow.id,
        version_id=db_version.id,
        name=new_name,
    )
