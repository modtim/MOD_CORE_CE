"""routers/workflow_breakpoints.py -- live-debugger sz-2.1b (opsx live-debugger).

CRUD routes over models.WorkflowBreakpoint (migrations/20261011h): per-user
breakpoints on one WorkflowBuilder canvas version. Breakpoints are per-user --
a list NEVER shows another user's rows -- and the partial unique
uq_workflow_breakpoints_live on (workflow_version_id, node_id, user_id) WHERE
deleted_at IS NULL makes "at most one live breakpoint per (version, node, user)"
a database fact, which the PUT upsert leans on: a losing race between two PUTs
is an IntegrityError, caught and resolved by re-reading and updating.

Authorization mirrors the workflow endpoints (routers/workflows_domains/
workflows.py): tenant-scoped workflow lookup + the per-workflow capability
gate `_user_can_access_workflow`. Foreign tenant / unknown workflow / a version
that does not belong to the workflow are ONE generic 404 -- no oracle.
PUT/DELETE additionally require edit OR execute on the workflow: a view-only
grantee gets 403 (they can list, they cannot set).

DELETE is a SOFT delete (ISO 27001 no-hard-delete): deleted_at + deleted_by,
row stays in the table; deleting nothing is an idempotent 204.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from crud import get_workflow_by_id
from dependencies import get_db, require_active_and_onboarded_user
from routers.workflows_domains._shared import _user_can_access_workflow
from models import WorkflowVersion

logger = logging.getLogger(__name__)

NOT_FOUND = "Workflow not found"
CONDITION_MAX_BYTES = 4096
NODE_ID_MAX_LEN = 255

router = APIRouter(prefix="/road", tags=["The Road", "Workflow Debug"])


class BreakpointRow(BaseModel):
    """One breakpoint row, serialized for the canvas (never a raw ORM row)."""
    model_config = ConfigDict(extra="forbid")

    id: int
    node_id: str
    enabled: bool
    condition: Optional[Dict[str, Any]] = None
    hit_count: int = 0
    last_hit_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BreakpointsListResponse(BaseModel):
    """GET list: the CALLER's live breakpoints on one canvas version."""
    model_config = ConfigDict(extra="forbid")

    workflow_id: int
    version_id: int
    breakpoints: List[BreakpointRow]


class BreakpointSetRequest(BaseModel):
    """PUT body: set/clear + the reserved condition predicate (NULL = always)."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    condition: Optional[Dict[str, Any]] = None


def _graph_node_ids(graph_json: Any) -> set:
    """Graph node ids of a version's graph_json (list-of-nodes or keyed map)."""
    if not isinstance(graph_json, dict):
        return set()
    nodes = graph_json.get("nodes") or []
    if isinstance(nodes, dict):
        return {str(k) for k in nodes.keys()}
    return {str(n["id"]) for n in nodes if isinstance(n, dict) and "id" in n}


def _live_breakpoint(db_session: Session, version_id: int, node_id: str,
                     user_id: int, tenant_id: int):
    """The ONE live row for (version, node, user) in tenant, or None."""
    return (
        db_session.query(models.WorkflowBreakpoint)
        .filter(models.WorkflowBreakpoint.workflow_version_id == version_id,
                models.WorkflowBreakpoint.node_id == node_id,
                models.WorkflowBreakpoint.user_id == user_id,
                models.WorkflowBreakpoint.tenant_id == tenant_id,
                models.WorkflowBreakpoint.deleted_at.is_(None))
        .first()
    )


def _workflow_and_version(db_session: Session, current_user, workflow_id: int,
                          version_id: int):
    """Tenant-scoped (workflow, version) pair or None (the generic-404 path).

    The version must belong to the workflow: a version id of ANOTHER workflow
    is reported exactly like an unknown workflow -- one 404, no oracle.
    """
    db_workflow = get_workflow_by_id(db_session, workflow_id=workflow_id,
                                     tenant_id=current_user.tenant_id)
    if db_workflow is None:
        return None
    db_version = db_session.get(WorkflowVersion, version_id)
    if (db_version is None or db_version.deleted_at is not None
            or db_version.workflow_id != workflow_id):
        return None
    return db_workflow, db_version


@router.get("/{workflow_id}/versions/{version_id}/breakpoints",
            response_model=BreakpointsListResponse)
def list_breakpoints(
    workflow_id: int,
    version_id: int,
    db_session: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """The current user's LIVE breakpoints on one canvas version.

    Per-user by design (the table is keyed by user): other users' rows on the
    same node/version are never listed. View access to the workflow is enough.
    """
    pair = _workflow_and_version(db_session, current_user, workflow_id, version_id)
    if pair is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    db_workflow, db_version = pair
    if not _user_can_access_workflow(db_session, current_user, db_workflow, "view"):
        raise HTTPException(status_code=404, detail=NOT_FOUND)

    rows = (
        db_session.query(models.WorkflowBreakpoint)
        .filter(models.WorkflowBreakpoint.workflow_version_id == version_id,
                models.WorkflowBreakpoint.user_id == current_user.id,
                models.WorkflowBreakpoint.tenant_id == current_user.tenant_id,
                models.WorkflowBreakpoint.deleted_at.is_(None))
        .order_by(models.WorkflowBreakpoint.id)
        .all()
    )
    return BreakpointsListResponse(
        workflow_id=workflow_id,
        version_id=version_id,
        breakpoints=[
            BreakpointRow(id=r.id, node_id=r.node_id, enabled=r.enabled,
                          condition=r.condition_json, hit_count=r.hit_count,
                          last_hit_at=r.last_hit_at, created_at=r.created_at,
                          updated_at=r.updated_at)
            for r in rows
        ],
    )


@router.put("/{workflow_id}/versions/{version_id}/breakpoints/{node_id}",
            response_model=BreakpointRow)
def set_breakpoint(
    workflow_id: int,
    version_id: int,
    node_id: str,
    body: BreakpointSetRequest,
    db_session: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Upsert the caller's live breakpoint on one graph node of one version.

    Edit OR execute on the workflow is required (a view-only grantee gets
    403, not 404: the workflow itself already passed the pair/tenant gate).
    The node must exist in the version's graph (else 422). A concurrent PUT
    losing on uq_workflow_breakpoints_live is an IntegrityError: we roll the
    failed insert back and re-read + update the winner's row instead of 500.
    """
    if len(node_id) > NODE_ID_MAX_LEN or any(ord(c) < 32 or ord(c) == 127
                                             for c in node_id):
        raise HTTPException(status_code=422, detail="invalid node_id")
    if not node_id:
        raise HTTPException(status_code=422, detail="invalid node_id")

    pair = _workflow_and_version(db_session, current_user, workflow_id,
                                 version_id)
    if pair is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    db_workflow, db_version = pair

    if not _user_can_access_workflow(db_session, current_user, db_workflow,
                                     "view"):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    can_edit = (_user_can_access_workflow(db_session, current_user, db_workflow,
                                          "edit")
                or _user_can_access_workflow(db_session, current_user,
                                             db_workflow, "execute"))
    if not can_edit:
        raise HTTPException(status_code=403,
                            detail="Insufficient permissions for this workflow")

    if node_id not in _graph_node_ids(db_version.graph_json):
        raise HTTPException(status_code=422, detail="node not in version graph")

    condition = body.condition
    if condition is not None:
        if len(json.dumps(condition)) > CONDITION_MAX_BYTES:
            raise HTTPException(status_code=422, detail="condition too large")

    row = _live_breakpoint(db_session, version_id, node_id, current_user.id,
                           current_user.tenant_id)
    if row is not None:
        row.enabled = body.enabled
        row.condition_json = condition
        row.updated_at = datetime.now(timezone.utc)
        db_session.commit()
        return _as_row(row)

    new_row = models.WorkflowBreakpoint(
        tenant_id=current_user.tenant_id, workflow_id=workflow_id,
        workflow_version_id=version_id, node_id=node_id,
        user_id=current_user.id, enabled=body.enabled,
        condition_json=condition)
    db_session.add(new_row)
    try:
        db_session.commit()
    except IntegrityError:
        # Lost a race on uq_workflow_breakpoints_live: the other PUT's row is
        # the live one -- roll back our insert and update THAT row.
        db_session.rollback()
        row = _live_breakpoint(db_session, version_id, node_id, current_user.id,
                               current_user.tenant_id)
        if row is None:
            raise HTTPException(status_code=500,
                                detail="breakpoint upsert failed")
        row.enabled = body.enabled
        row.condition_json = condition
        row.updated_at = datetime.now(timezone.utc)
        db_session.commit()
        return _as_row(row)
    db_session.refresh(new_row)
    return _as_row(new_row)


def _as_row(row) -> BreakpointRow:
    """ORM row -> response model (explicit fields only)."""
    return BreakpointRow(id=row.id, node_id=row.node_id, enabled=row.enabled,
                         condition=row.condition_json, hit_count=row.hit_count,
                         last_hit_at=row.last_hit_at, created_at=row.created_at,
                         updated_at=row.updated_at)


@router.delete("/{workflow_id}/versions/{version_id}/breakpoints/{node_id}",
               status_code=204, response_model=None)
def clear_breakpoint(
    workflow_id: int,
    version_id: int,
    node_id: str,
    db_session: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Soft-delete the caller's live breakpoint (ISO: tombstone, never drop).

    Idempotent: no live row -> 204 as well. Same pair/tenant 404 gate and the
    same edit-or-execute 403 gate as PUT.
    """
    if len(node_id) > NODE_ID_MAX_LEN or any(ord(c) < 32 or ord(c) == 127
                                             for c in node_id):
        raise HTTPException(status_code=422, detail="invalid node_id")
    if not node_id:
        raise HTTPException(status_code=422, detail="invalid node_id")

    pair = _workflow_and_version(db_session, current_user, workflow_id,
                                 version_id)
    if pair is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    db_workflow, _db_version = pair

    if not _user_can_access_workflow(db_session, current_user, db_workflow,
                                     "view"):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    can_edit = (_user_can_access_workflow(db_session, current_user, db_workflow,
                                          "edit")
                or _user_can_access_workflow(db_session, current_user,
                                             db_workflow, "execute"))
    if not can_edit:
        raise HTTPException(status_code=403,
                            detail="Insufficient permissions for this workflow")

    row = _live_breakpoint(db_session, version_id, node_id, current_user.id,
                           current_user.tenant_id)
    if row is None:
        return None
    row.deleted_at = datetime.now(timezone.utc)
    row.deleted_by = current_user.id
    db_session.commit()
    return None
