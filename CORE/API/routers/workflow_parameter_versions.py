"""expr 10.5 (workflow-expressiveness-primitives) — parameter-version history + rollback.

Routes (mounted under /v1 in main.py):
    GET  /v1/road/{workflow_id}/parameter-versions?workflow_version_id=&limit=
    POST /v1/road/{workflow_id}/parameter-versions/{version_id}/rollback

GET 200: {"items": [...]} — newest-first rows from services.workflow_parameter_versions
.list_versions (soft-deleted rows excluded, `workflow_version_id` narrows to ONE
graph, `limit` clamped 1..200 by the service).

POST 201: {"id": <new row id>, "rolled_back_to": <version_id>} — rollback APPENDS a
new source='rollback' row (the service never commits, so the router commits ONCE
here on success); the target and every older row stay byte-identical (append-only,
ISO 27001: this endpoint never deletes, soft or hard).

Errors are ONE generic shape: 404 {"error": "not_found"} for an unknown workflow,
another tenant's workflow, a soft-deleted workflow, or a missing/foreign/soft-deleted
target version — no oracle into which of the four it was. A caller who can VIEW but
not EDIT the workflow gets 403 on rollback (they can read the history): the workflow
already passed the tenant/404 gate, so the permission gap is not a secrecy question.

Authorization mirrors routers/workflow_breakpoints.py: tenant-scoped workflow lookup
(crud/workflow_cud.get_workflow_by_id) + the per-workflow capability gate
`_user_can_access_workflow(db, current_user, wf, "view"|"edit")`. Tenant_id comes
STRICTLY from the auth token, never from the path/body.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from crud import workflow_cud as wf_crud
from dependencies import get_db, require_bearer_or_api_key
from routers.workflows_domains._shared import _user_can_access_workflow
import services.workflow_parameter_versions as wv

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/road", tags=["The Road", "Workflow Parameter Versions"])

NOT_FOUND = "not_found"
FORBIDDEN = "forbidden"

# The service clamps the limit itself (_LIMIT_MIN/_LIMIT_MAX); these mirror it so
# the router's contract is documented in one place.
LIMIT_DEFAULT = 50
LIMIT_MIN, LIMIT_MAX = 1, 200


def _not_found() -> JSONResponse:
    """One generic 404 body — unknown/foreign/soft-deleted are indistinguishable."""
    return JSONResponse(status_code=404, content={"error": NOT_FOUND})


class _ListOut(BaseModel):
    """200 shape for the history list (rows are the service's dicts)."""
    items: List[Dict[str, Any]]


class _RollbackOut(BaseModel):
    """201 shape: the NEW appended row's id + the target it copied."""
    id: int
    rolled_back_to: int


@router.get("/{workflow_id}/parameter-versions", response_model=_ListOut)
def list_parameter_versions(
    workflow_id: int,
    workflow_version_id: Optional[int] = None,
    limit: int = LIMIT_DEFAULT,
    db_session: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
) -> JSONResponse:
    """Newest-first parameter history for one workflow of the caller's tenant.

    `workflow_version_id` narrows to one graph; `limit` is clamped to 1..200.
    View access to the workflow is required; foreign/unknown/soft-deleted -> 404.
    """
    db_workflow = wf_crud.get_workflow_by_id(db_session, workflow_id=workflow_id,
                                             tenant_id=current_user.tenant_id)
    if db_workflow is None:
        return _not_found()
    if not _user_can_access_workflow(db_session, current_user, db_workflow, "view"):
        return _not_found()

    items = wv.list_versions(db_session, tenant_id=current_user.tenant_id,
                             workflow_id=workflow_id,
                             workflow_version_id=workflow_version_id,
                             limit=limit)
    return {"items": items}


@router.post("/{workflow_id}/parameter-versions/{version_id}/rollback",
             response_model=_RollbackOut, status_code=201)
def rollback_parameter_version(
    workflow_id: int,
    version_id: int,
    db_session: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
) -> JSONResponse:
    """Append a copy of `version_id`'s parameters as the newest row (201).

    Edit permission on the workflow is required (a view-only grantee gets 403).
    The service refuses a missing/foreign/soft-deleted target or one that belongs
    to another workflow -> 404. It never commits, so the router commits ONCE here.
    """
    db_workflow = wf_crud.get_workflow_by_id(db_session, workflow_id=workflow_id,
                                             tenant_id=current_user.tenant_id)
    if db_workflow is None:
        return _not_found()
    if not _user_can_access_workflow(db_session, current_user, db_workflow, "view"):
        return _not_found()
    if not _user_can_access_workflow(db_session, current_user, db_workflow, "edit"):
        return JSONResponse(status_code=403, content={"error": FORBIDDEN})

    new_id = wv.rollback_to(db_session, tenant_id=current_user.tenant_id,
                             workflow_id=workflow_id, target_version_id=version_id,
                             created_by=current_user.id)
    if new_id is None:
        return _not_found()
    db_session.commit()
    return JSONResponse(status_code=201,
                        content={"id": new_id, "rolled_back_to": version_id})
