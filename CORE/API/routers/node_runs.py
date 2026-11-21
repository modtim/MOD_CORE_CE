# Auto-generated domain sub-module (Gate 2a split of routers/node_runs.py).
# Behavior-preserving: endpoints MOVED verbatim, grouped by audit domain.
from typing import List
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

import crud, models, schemas
from dependencies import get_db, require_active_and_onboarded_user, get_impersonated_user_from_service_account, require_service_account_token, require_bearer_or_api_key, get_tenant_minio_client
from utils.run_task_auth import node_or_service_principal, pin_run_task_tenant
from utils.minio_client import MinioClient

from routers.node_runs_domains._shared import *  # re-export helpers/constants
from routers.node_runs_domains import (
    node_authoring,
    assets_cogs_files,
    task_queue_scheduler,
    node_runs,
)

router = APIRouter(
    tags=["Node Runs"],
    responses={404: {"description": "Not found"}},
)

service_router = APIRouter(
    prefix="/service",
    tags=["Node Runs (Service)"],
    responses={404: {"description": "Not found"}},
)

router.include_router(node_authoring.router)
router.include_router(node_runs.router)
service_router.include_router(assets_cogs_files.service_router)
service_router.include_router(task_queue_scheduler.service_router)

# Re-export handlers so `from routers.node_runs import <fn>` resolves as before.
from routers.node_runs_domains.node_runs import get_node_fanout
from routers.node_runs_domains.task_queue_scheduler import get_node_run_by_node_id
from routers.node_runs_domains.node_runs import get_node_run_previews
from routers.node_runs_domains.task_queue_scheduler import get_node_run_resources_for_workflow_run_service
from routers.node_runs_domains.node_authoring import get_node_runs_for_workflow_run
from routers.node_runs_domains.assets_cogs_files import get_node_runs_for_workflow_run_service
from routers.node_runs_domains.node_runs import get_node_versions
from routers.node_runs_domains.node_runs import get_run_fanout_summary
from routers.node_runs_domains.task_queue_scheduler import update_node_run_by_node_id
from routers.node_runs_domains.node_runs import update_node_run_status
