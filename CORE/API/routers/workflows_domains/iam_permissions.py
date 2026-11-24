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
    """Validate a ZIP workflow bundle before importing."""
    contents = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(contents), "r")
        manifest = json.loads(zf.read("workflow.json"))
        zf.close()
    except Exception as e:
        logger.error("Invalid ZIP: %s", e)
        raise HTTPException(status_code=400, detail="Invalid ZIP file")

    # F12: a workflow.json holding a JSON list/string used to hit
    # `manifest.get(...)` inside the try and be mislabelled "Invalid ZIP file"
    # (or, for other shapes, escape as a 500). The archive is fine; the user's
    # manifest is wrong -> 400 with the real reason.
    if not isinstance(manifest, dict):
        raise HTTPException(
            status_code=400,
            detail="invalid workflow.json: expected a JSON object",
        )

    required_nodes_list = manifest.get("required_nodes", [])

    return _validate_required_nodes(db, current_user.tenant_id, required_nodes_list)
