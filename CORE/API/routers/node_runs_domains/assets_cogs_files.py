# Auto-generated domain sub-module (Gate 2a split of routers/node_runs.py).
# Behavior-preserving: endpoints MOVED verbatim, grouped by audit domain.
# Gate 2b: handler bodies now delegate to
# services/node_runs_assets_cogs_files_service.py +
# crud/node_runs_assets_cogs_files.py (query layer). Signatures/decorators/
# docstrings/tags are UNCHANGED so the contract snapshot stays byte-identical.
from typing import List
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

import crud, models, schemas
from dependencies import get_db, require_active_and_onboarded_user, get_impersonated_user_from_service_account, require_service_account_token, require_bearer_or_api_key, get_tenant_minio_client
from utils.run_task_auth import node_or_service_principal, pin_run_task_tenant
from utils.minio_client import MinioClient

from routers.node_runs_domains._shared import *

from services import node_runs_assets_cogs_files_service as _acfs
from services._base import NotFoundError as _NotFoundError

service_router = APIRouter()


@service_router.get("/workflow/{run_id}", response_model=List[schemas.NodeRun])
def get_node_runs_for_workflow_run_service(
    run_id: int,
    db: Session = Depends(get_db),
    principal=Depends(node_or_service_principal),
):
    """
    Retrieve all node runs associated with a specific workflow run.
    Service account endpoint for use by DAGs.
    """
    try:
        return _acfs.get_node_runs_for_workflow_run(db, run_id, principal)
    except _NotFoundError as e:
        raise HTTPException(status_code=404, detail=e.message)
