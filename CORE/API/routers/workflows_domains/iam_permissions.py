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
logger = themed_logger('routers.workflows_domains.iam_permissions', 'workflows')
from pydantic import BaseModel
import json
import io
import re
import zipfile
from dependencies import require_bearer_or_api_key
from utils.run_task_auth import node_or_onboarded_user_principal, pin_run_task_tenant

from routers.workflows_domains._shared import *

router = APIRouter()


@router.post("/validate-import-zip")
async def validate_workflow_import_zip(
        file: UploadFile = File(...),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Validate a ZIP workflow bundle before importing.

    VALIDATE-IMPORT-ZIP-REWORK: the bundle checks are the SAME shared, pure
    validator POST /road/import-zip runs (_validate_import_zip_bundle in
    _shared.py), so this route rejects exactly what the import route rejects —
    same status codes, same `detail` text the Import dialog shows verbatim
    (useWorkflowApi.ts importFailureReason): non-.zip filename (400), unreadable
    ZIP (400), zip-slip / symlink / zip-bomb entries (422, GSEC R-007),
    missing workflow.json (400), invalid JSON (400), manifest not an object
    (400), missing graph_json (400), non-UTF-8 DAG .py (422), non-literal-manifest
    DAG .py (422). Nothing is written and no zip-entry rule is weakened: the
    validation is pure. On top of the rejections this route keeps its own job —
    the required_nodes availability report.
    """
    contents = await file.read()
    manifest, _dags = _validate_import_zip_bundle(file.filename, contents, db)

    required_nodes_list = manifest.get("required_nodes", [])

    return _validate_required_nodes(db, current_user.tenant_id, required_nodes_list)
