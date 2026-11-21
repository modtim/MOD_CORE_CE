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
import logging
logger = logging.getLogger(__name__)
from pydantic import BaseModel
import json
import io
import re
import zipfile
from dependencies import require_bearer_or_api_key
from utils.run_task_auth import node_or_onboarded_user_principal, pin_run_task_tenant

from routers.workflows_domains._shared import *  # re-export helpers/constants
from routers.workflows_domains import (
    node_authoring,
    assets_cogs_files,
    workflows,
    iam_permissions,
    task_queue_scheduler,
    workflow_webhook_subscriptions,
    workflow_by_slug,
)

# billing_plans (credit estimation / plan gating) is a Paid sub-module: present
# in the full build, absent in CE. Import + mount it only if present so this
# aggregator boots in CE. No-op when the module IS present.
try:
    from routers.workflows_domains import billing_plans
except ImportError:  # CE build: Paid sub-module absent
    billing_plans = None

router = APIRouter(tags=["Workflows", "The Road"])
workflow_runner_router = APIRouter(
    prefix="/workflow-runner",
    tags=["Workflow Runner (MCP)"],
)
service_router = APIRouter(
    tags=["Workflows - Service", "The Road"],
    prefix=""
)

router.include_router(node_authoring.router, tags=["The Road"])
router.include_router(assets_cogs_files.router, tags=["The Road"])
# c9-J3: before workflows.router so /by-slug/{slug} never hits /{workflow_id}
router.include_router(workflow_by_slug.router, tags=["The Road"])
router.include_router(workflows.router, tags=["The Road"])
if billing_plans is not None:
    router.include_router(billing_plans.router, tags=["The Road"])
router.include_router(iam_permissions.router, tags=["The Road"])
# GX.R3 — run-completion webhook subscriptions (per-workflow, HMAC-signed).
router.include_router(workflow_webhook_subscriptions.router, tags=["The Road"])
service_router.include_router(assets_cogs_files.service_router, tags=["The Road"])
workflow_runner_router.include_router(task_queue_scheduler.workflow_runner_router)

# Re-export every handler so `from routers.workflows import <fn>` and
# monkeypatch targets (routers.workflows.<fn>) resolve exactly as before.
from routers.workflows_domains.workflows import archive_workflow
from routers.workflows_domains.workflows import batch_update_processing_history
from routers.workflows_domains.workflows import cancel_workflow_run
from routers.workflows_domains.node_authoring import create_workflow
from routers.workflows_domains.workflows import create_workflow_version
from routers.workflows_domains.assets_cogs_files import delete_workflow
from routers.workflows_domains.workflows import delete_workflow_version
from routers.workflows_domains.assets_cogs_files import duplicate_workflow
from utils.paid_import import optional_paid
_billing_plans = optional_paid("routers.workflows_domains.billing_plans")
if _billing_plans is not None:
    estimate_workflow_credits = _billing_plans.estimate_workflow_credits
from routers.workflows_domains.assets_cogs_files import exchange_workflow_token
from routers.workflows_domains.workflows import export_workflow
from routers.workflows_domains.workflows import get_asset_lineage
from routers.workflows_domains.workflows import get_cost_breakdown_by_node
from routers.workflows_domains.workflows import get_node_controls
from routers.workflows_domains.workflows import get_node_execution_status
from routers.workflows_domains.workflows import get_processing_history
from routers.workflows_domains.workflows import get_quality_metrics_aggregation
from routers.workflows_domains.assets_cogs_files import get_queue_status
from routers.workflows_domains.workflows import get_resource_usage_analytics
from routers.workflows_domains.assets_cogs_files import get_workflow
from routers.workflows_domains.workflows import get_workflow_detailed_log
from routers.workflows_domains.workflows import get_workflow_output_folders
from routers.workflows_domains.workflows import get_workflow_progress
from routers.workflows_domains.assets_cogs_files import get_workflow_run_details
from routers.workflows_domains.workflows import get_workflow_run_status
from routers.workflows_domains.workflows import get_workflow_runs_grid
from routers.workflows_domains.assets_cogs_files import get_workflow_variables
from routers.workflows_domains.workflows import get_workflow_version
from routers.workflows_domains.assets_cogs_files import get_workflow_version_runs
from routers.workflows_domains.workflows import import_workflow
from routers.workflows_domains.workflows import import_workflow_zip
from routers.workflows_domains.assets_cogs_files import list_all_workflow_runs
from routers.workflows_domains.workflows import list_workflow_runs
from routers.workflows_domains.workflows import list_workflow_versions
from routers.workflows_domains.node_authoring import list_workflows
from routers.workflows_domains.workflows import node_run_action
from routers.workflows_domains.workflows import pause_node_in_run
from routers.workflows_domains.workflows import pause_workflow_run
from routers.workflows_domains.workflows import predict_workflow_cost
from routers.workflows_domains.assets_cogs_files import request_workflow_access
from routers.workflows_domains.workflows import restart_workflow_run
from routers.workflows_domains.workflows import resume_node_in_run
from routers.workflows_domains.workflows import resume_workflow_run
from routers.workflows_domains.workflows import search_by_processing
from routers.workflows_domains.workflows import set_workflow_schedule_paused
from routers.workflows_domains.workflows import trigger_workflow_run
from routers.workflows_domains.workflows import unarchive_workflow
from routers.workflows_domains.assets_cogs_files import update_workflow
from routers.workflows_domains.workflows import validate_workflow_import
from routers.workflows_domains.iam_permissions import validate_workflow_import_zip
from routers.workflows_domains.task_queue_scheduler import wr_cancel_run
from routers.workflows_domains.task_queue_scheduler import wr_restart_run
from routers.workflows_domains.task_queue_scheduler import wr_run_cost
from routers.workflows_domains.task_queue_scheduler import wr_run_node_status
from routers.workflows_domains.task_queue_scheduler import wr_run_progress
from routers.workflows_domains.task_queue_scheduler import wr_run_status
from routers.workflows_domains.task_queue_scheduler import wr_run_version
