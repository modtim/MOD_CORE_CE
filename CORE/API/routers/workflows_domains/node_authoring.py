# Auto-generated domain sub-module (Gate 2a split of routers/workflows.py).
# Behavior-preserving: endpoints MOVED verbatim, grouped by audit domain.
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, status, HTTPException, BackgroundTasks, Body, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import cast, String
import os
import schemas, crud, models
from dependencies import get_db, require_active_and_onboarded_user, get_keycloak_token, require_harbor_credentials, require_workflow_run_key
try:
    from utils.pricing import get_postpaid_credit_price, get_overage_credit_price
except ImportError:
    def get_postpaid_credit_price(*args, **kwargs):
        return 0

    def get_overage_credit_price(*args, **kwargs):
        return 0
from product_names import themed_logger
logger = themed_logger('routers.workflows_domains.node_authoring', 'workflows')
from pydantic import BaseModel
import json
import io
import re
import zipfile
from dependencies import require_bearer_or_api_key
from utils.run_task_auth import node_or_onboarded_user_principal, pin_run_task_tenant

from routers.workflows_domains._shared import *
from utils.tenant_lifecycle import require_tenant_active, require_tenant_writable

router = APIRouter()


@router.post("/", response_model=schemas.Workflow, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_tenant_writable)])
def create_workflow(
        workflow: schemas.WorkflowCreate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Creates a new workflow container. Does not create a version.
    """
    try:
        db_workflow = models.Workflow()
        db_workflow.name = workflow.name
        db_workflow.created_by_user_id = current_user.id
        db_workflow.tenant_id = current_user.tenant_id
        db.add(db_workflow)
        db.flush()

        from resource_permissions import grant_owner_permissions
        grant_owner_permissions(db, current_user.tenant_id, current_user.id, models.ResourceType.WORKFLOW, db_workflow.id)
        db.commit()
        db.refresh(db_workflow)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Owner grant failed: {e}")

    return db_workflow

@router.get("/", response_model=List[schemas.Workflow],
            dependencies=[Depends(require_tenant_active)])
def list_workflows(
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "name",
        order: str = "asc",
        include_system: bool = False,
        include_archived: bool = False,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Lists all workflows for the tenant, with pagination, filtering, and sorting.

    Platform-internal workflows (Node Wizard build pipelines, system
    maintenance) are hidden by default — pass `include_system=true` to
    see them. Archived workflows are hidden by default too — pass
    `include_archived=true` to include them.
    """
    workflows = crud.get_workflows_for_tenant(
        db=db,
        tenant_id=current_user.tenant_id,
        search=search,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        order=order,
        include_system=include_system,
        include_archived=include_archived,
        viewer_user=current_user,
    )
    # GY.C7 — batched effective labels (each workflow resolves via its most
    # recent run's input cogs). Set on the transient instances for the
    # response; fail-OPEN so a lookup hiccup never 500s the list.
    if workflows:
        try:
            from services import data_classification as _gy_c7_dc
            labels = _gy_c7_dc.workflow_labels_for_ids(
                db, [w.id for w in workflows], tenant_id=current_user.tenant_id)
            for w in workflows:
                w.data_classification = labels.get(w.id)
        except Exception:
            pass
    return workflows
