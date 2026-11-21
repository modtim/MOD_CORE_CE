"""WSIP 5.5/5.6 — HTTP surface for the causal-graph read wrappers.

Routes (mounted under /v1 by the lead; NOT mounted here):
    GET /v1/road/{workflow_id}/root-cause?node_id=
    GET /v1/road/{workflow_id}/counterfactual?node_id=

Both are READ-ONLY: they delegate to the tenant-scoped wrappers in
services/causal_graph.py (:func:`root_cause_for_workflow`,
:func:`counterfactual_for_workflow`) and never write, commit, or create a
version. Every answer is tagged ``"structural-only"`` by the service (the
empirical feature-level layer is 5.3/5.4).

Feature gate: ``wsip.conversational_debug_enabled`` (services/wsip_feature_flags.py
read helper, fail-closed). OFF for the caller's tenant ->
403 {"error": "feature_disabled"} — the calibration-router convention
(routers/failure_prediction_calibration.py: a disabled feature is a real
HTTP status, not a missing surface; routers/autonomous_proposals.py instead
answers 404, which the brief says is wrong here).

Tenant isolation: the caller's tenant_id comes from the auth token; another
tenant's workflow, a soft-deleted workflow and a nonexistent id all answer
the identical 404 {"error": "not_found"} so existence never leaks.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_bearer_or_api_key
from services import wsip_feature_flags as wsip
try:
    from services.causal_graph import (
        counterfactual_for_workflow,
        root_cause_for_workflow,
    )
except ImportError:
    counterfactual_for_workflow = None
    root_cause_for_workflow = None

logger = logging.getLogger(__name__)

NOT_FOUND = "not_found"
FEATURE_DISABLED = "feature_disabled"
NODE_ID_MAX_LEN = 200

router = APIRouter(prefix="/road", tags=["The Road", "Workflow Causal Graph"])


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class _EmpiricalEdgeOut(BaseModel):
    """One empirical-layer edge with its own confidence (spec: "confidence per edge")."""

    source_node_id: str
    target_node_id: str
    source_feature: Optional[str] = None
    target_feature: Optional[str] = None
    sample_count: Optional[int] = None
    confidence: Optional[float] = None


class _RootCauseOut(BaseModel):
    found: bool
    workflow_id: int
    terminal_node_id: str
    terminal_feature: Optional[str] = None
    root_causes: List[str]
    # "structural-only" when the empirical layer has no live edges; "hybrid"
    # when causal_graph_edges carries empirical rows for this workflow.
    mode: str
    caveats: List[str]
    confidence: Optional[float] = None
    empirical_edges: List[_EmpiricalEdgeOut] = []


class _CounterfactualOut(BaseModel):
    found: bool
    workflow_id: int
    modified_node_id: str
    modified_feature: Optional[str] = None
    affected_terminals: List[str]
    mode: str
    caveats: List[str]
    confidence: Optional[float] = None
    empirical_edges: List[_EmpiricalEdgeOut] = []


# ---------------------------------------------------------------------------
# Stable error bodies (returned, not raised: a JSONResponse bypasses
# response_model so the body is exactly {"error": <code>})
# ---------------------------------------------------------------------------


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": code})


def _feature_disabled_response() -> JSONResponse:
    """403 while the tenant's WSIP conversational-debug flag is off.

    A disabled feature is not a missing surface (404 would claim the workflow
    does not exist); it is a forbidden one. Mirrors the
    routers/failure_prediction_calibration.py posture of a real HTTP status
    for a gate, with the {"error": ...} body shape the WSIP routers use.
    """
    return _error(403, FEATURE_DISABLED)


def _not_found() -> JSONResponse:
    """404 for a missing row AND for another tenant's row: existence never
    leaks (routers/autonomous_proposals.py posture)."""
    return _error(404, NOT_FOUND)


def _node_id_missing() -> JSONResponse:
    """422 when node_id is absent or empty."""
    return _error(422, "node_id_required")


def _node_id_too_long() -> JSONResponse:
    """422 when node_id exceeds NODE_ID_MAX_LEN."""
    return _error(422, "node_id_too_long")


def _flag_on(db: Session, tenant_id: Optional[int]) -> bool:
    """The tenant's WSIP conversational-debug flag, fail-closed (helper's default)."""
    return wsip.conversational_debug_enabled(db, tenant_id)


def _node_id_error(node_id: Optional[str]):
    """None when node_id is usable; else the stable 422 body.

    Absent/blank -> node_id_required; over NODE_ID_MAX_LEN -> node_id_too_long.
    """
    if not isinstance(node_id, str) or not node_id:
        return _node_id_missing()
    if len(node_id) > NODE_ID_MAX_LEN:
        return _node_id_too_long()
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{workflow_id}/root-cause",
    response_model=_RootCauseOut,
    summary="Ranked structural ancestors of a node (WSIP 5.5)",
)
def get_root_cause(
    workflow_id: int,
    node_id: Optional[str] = Query(
        None,
        alias="node_id",
        description="Terminal node to explain (required, <=200 chars).",
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
) -> Any:
    """Root-cause ranking for the CALLER's tenant only.

    ``found`` False (cross-tenant / soft-deleted / unknown workflow) -> 404
    {"error": "not_found"}. An UNKNOWN node_id in a live workflow is not an
    error: the service ranks nothing, so the answer is 200 with
    ``root_causes: []`` (documented: existence of a node is never asserted
    either way, and a root node legitimately answers the same way).
    """
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="No tenant for this caller")
    if not _flag_on(db, tenant_id):
        return _feature_disabled_response()
    if root_cause_for_workflow is None:
        return _feature_disabled_response()
    bad_node = _node_id_error(node_id)
    if bad_node is not None:
        return bad_node

    result = root_cause_for_workflow(
        db,
        workflow_id=workflow_id,
        terminal_node_id=node_id,
        tenant_id=tenant_id,
    )
    if not result.get("found"):
        return _not_found()
    return _RootCauseOut(**result)


@router.get(
    "/{workflow_id}/counterfactual",
    response_model=_CounterfactualOut,
    summary="Terminals downstream of a node (WSIP 5.6)",
)
def get_counterfactual(
    workflow_id: int,
    node_id: Optional[str] = Query(
        None,
        alias="node_id",
        description="Modified node id (required, <=200 chars).",
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
) -> Any:
    """Counterfactual surface for the CALLER's tenant only.

    Same mapping as root-cause: unknown workflow -> 404 {"error": "not_found"};
    an unknown node_id answers 200 with ``affected_terminals: []``.
    """
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="No tenant for this caller")
    if not _flag_on(db, tenant_id):
        return _feature_disabled_response()
    if counterfactual_for_workflow is None:
        return _feature_disabled_response()
    bad_node = _node_id_error(node_id)
    if bad_node is not None:
        return bad_node

    result = counterfactual_for_workflow(
        db,
        workflow_id=workflow_id,
        modified_node_id=node_id,
        tenant_id=tenant_id,
    )
    if not result.get("found"):
        return _not_found()
    return _CounterfactualOut(**result)
