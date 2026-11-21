# Auto-generated domain sub-module (Gate 2a split of routers/node_runs.py).
# Behavior-preserving: endpoints MOVED verbatim, grouped by audit domain.
# Gate 2b migration (api-refactor opsx): the query moved to
# crud/node_runs_node_authoring.py, the tenant/view-gate policy to
# services/node_runs_node_authoring_service.py. Handler is a thin delegation.
from typing import List
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

import crud, models, schemas
from dependencies import get_db, require_active_and_onboarded_user, get_impersonated_user_from_service_account, require_service_account_token, require_bearer_or_api_key, get_tenant_minio_client
from services import node_runs_node_authoring_service
from services._base import NotFoundError
from utils.run_task_auth import node_or_service_principal, pin_run_task_tenant
from utils.minio_client import MinioClient

from routers.node_runs_domains._shared import *

router = APIRouter()


@router.get("/{run_id}", response_model=List[schemas.NodeRun])
def get_node_runs_for_workflow_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Retrieve all node runs associated with a specific workflow run.
    Includes workflow_node relationship for status updates in the UI.
    """
    try:
        return node_runs_node_authoring_service.get_node_runs_for_workflow_run(
            db, run_id, current_user
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
