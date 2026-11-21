"""routers/workflows_debug.py -- live-debugger sz-2.6 (opsx live-debugger).

GET  /v1/road/workflow-runs/{run_id}/debug/frame/{node_id}  -- read-only
     frame_state for one node of one run (what the run would execute with if it
     resumed there: config, port inputs, parents' outputs, partial output, the
     worker holding a task).

Authorization is copied EXACTLY from the run-detail endpoint
`GET /v1/road/workflow-runs/{run_id}` (routers/workflows_domains/
assets_cogs_files.py::get_workflow_run_details): tenant-scoped lookup (no
platform-admin widening, ISO) + the `_user_can_view_run` per-workflow view
scope, both hiding foreign/ungranted runs as a single generic 404 -- no
oracle telling the caller which half of the pair they got wrong.

services/live_debugger.inspect_frame is the read half of the vertical: it
performs no writes and no commits, so this route is safe while a paused run is
mid-flight. LookupError from it maps to the same generic 404.

--------------------------------------------------------------------------------
-- sz-2.4 (WebSocket live-debugger attach) LANDS IN THIS FILE -- add its routes
-- below this section marker; the frame GET above is its read-only twin.
--------------------------------------------------------------------------------
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

import models
from crud import get_workflow_run_by_id
from dependencies import get_db, require_active_and_onboarded_user
from routers.workflows_domains._shared import _user_can_view_run
from utils.paid_import import optional_paid
live_debugger = optional_paid("services.live_debugger")

logger = logging.getLogger(__name__)

RUN_NOT_FOUND = "Workflow run not found"

router = APIRouter(prefix="/road", tags=["The Road", "Workflow Debug"])


class DebugFrameResponse(BaseModel):
    run_id: int
    node_id: str
    paused: bool
    node_kind: Optional[str] = None
    node_definition_id: Optional[int] = None
    config: Dict[str, Any] = {}
    inputs: Dict[str, Any] = {}
    upstream_outputs: List[Dict[str, Any]] = []
    partial_output: Optional[Any] = None
    worker_context: Optional[Dict[str, Any]] = None


@router.get("/workflow-runs/{run_id}/debug/frame/{node_id}",
            response_model=DebugFrameResponse)
def get_debug_frame(
    run_id: int,
    node_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Read-only debug frame for one node of one run (live-debugger sz-2.6).

    Same authorization as viewing the run itself: the run must belong to the
    caller's tenant and its workflow must be in the caller's view scope. A run
    that fails either check -- and a node that is not part of the run's own
    graph -- is reported as the same generic 404.
    """
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening

    db_run = get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)

    if live_debugger is None:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)
    try:
        return live_debugger.inspect_frame(
            db,
            tenant_id=tenant_filter,
            run_id=run_id,
            node_id=node_id,
        )
    except LookupError:
        # A foreign run is indistinguishable from a missing one, and a node
        # outside the run's graph says nothing more than "not found" either.
        logger.info("debug frame %s/%s not found for tenant %s",
                    run_id, node_id, tenant_filter)
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)


class HotSwapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: Optional[Dict[str, Any]] = None
    node_definition_id: Optional[int] = None


class HotSwapOverride(BaseModel):
    config: Dict[str, Any] = {}
    node_definition_id: Optional[int] = None
    swapped_at: Optional[str] = None
    swapped_by: Optional[int] = None


class HotSwapResponse(BaseModel):
    run_id: int
    node_id: str
    override: HotSwapOverride


@router.post("/workflow-runs/{run_id}/debug/hot-swap/{node_id}",
             response_model=HotSwapResponse, status_code=200)
def hot_swap_debug_node(
    run_id: int,
    node_id: str,
    payload: HotSwapRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Store a per-run override on a paused, not-yet-dispatched node (sz-2.7.2).

    Same authorization as viewing the run (tenant scope + per-workflow view
    scope, both folded into one generic 404) -- the run lookup is tenant-scoped
    BEFORE the service is touched, so a foreign run never reaches it.

    ``services/live_debugger.breakpoint_service.hot_swap`` does the write:
    ``LookupError`` (run/node/definition not in scope) -> the same generic 404
    the frame GET uses; ``RuntimeError`` (node not paused, node already
    dispatched) -> 409 carrying the service's own message; ``ValueError``
    (nothing to swap, malformed/oversized config) -> 422. The returned dict is
    already secret-masked by the service.
    """
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening

    db_run = get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)

    if live_debugger is None:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)
    try:
        result = live_debugger.breakpoint_service.hot_swap(
            db,
            tenant_id=tenant_filter,
            run_id=run_id,
            node_id=node_id,
            actor_user_id=current_user.id,
            config=payload.config,
            node_definition_id=payload.node_definition_id,
        )
    except LookupError:
        logger.info("debug hot-swap %s/%s not found for tenant %s",
                    run_id, node_id, tenant_filter)
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)
    except RuntimeError as refused:
        # The service's own refusal text ("node is not paused" / "node already
        # dispatched") is curated, not raw exception text -- same shape as the
        # platform-elevation refusals.
        raise HTTPException(status_code=409, detail=str(refused))
    except ValueError as bad:
        raise HTTPException(status_code=422, detail=str(bad))

    return result


class StepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["over", "into"] = "over"


class StepResponse(BaseModel):
    workflow_run_id: int
    stepped: str
    next_paused: List[str]
    paused_nodes: List[str]
    mode: Optional[str] = None


@router.post("/workflow-runs/{run_id}/debug/step/{node_id}",
             response_model=StepResponse, status_code=200)
def step_debug_node(
    run_id: int,
    node_id: str,
    payload: StepRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Step over or into one paused node (sz-2.8.2).

    Same authorization as viewing the run (tenant scope + per-workflow view
    scope, both folded into one generic 404) -- the run lookup is tenant-scoped
    BEFORE the service is touched, so a foreign run never reaches it.

    ``services.live_debugger.breakpoint_service.step_over`` / ``step_into``
    perform the write: ``RuntimeError`` (node not paused, node already
    dispatched) -> 409 carrying the service's own message;
    ``ValueError`` (step_into on sub_workflow) -> 422; ``LookupError`` (run/node
    not in scope) -> same generic 404.

    The frontier is re-kicked after the state change (like resume). Re-kick
    failure does NOT fail the request because the state change is already
    committed.
    """
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening

    db_run = get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if not db_run:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)

    if live_debugger is None:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)

    step_mode = payload.mode or "over"

    try:
        if step_mode == "into":
            result = live_debugger.breakpoint_service.step_into(
                db,
                tenant_id=tenant_filter,
                run_id=run_id,
                node_id=node_id,
                actor_user_id=current_user.id,
            )
        else:
            result = live_debugger.breakpoint_service.step_over(
                db,
                tenant_id=tenant_filter,
                run_id=run_id,
                node_id=node_id,
                actor_user_id=current_user.id,
            )
    except LookupError:
        logger.info("debug step %s/%s not found for tenant %s",
                    run_id, node_id, tenant_filter)
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)
    except RuntimeError as refused:
        raise HTTPException(status_code=409, detail=str(refused))
    except ValueError as bad:
        raise HTTPException(status_code=422, detail=str(bad))

    # 2400LR-bp2: a held ENTRY root has no completed upstream to re-kick it --
    # dispatch it exactly like resume-node does (guard dedupes a second step).
    try:
        from services.workflow_orchestrator import dispatch_released_root_node
        if dispatch_released_root_node(db, db_run, node_id):
            db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("step: entry-node dispatch failed for run %s: %s", run_id, e)

    # Re-kick the frontier so children dispatch if their parents are done.
    # Re-kick failure does NOT fail the request: the state change is already
    # committed above.  Mirrors resume-node's re-kick pattern verbatim.
    try:
        from routers.worker_tasks import _check_and_dispatch_downstream
        from crud import workflows_domain as workflows_domain_crud
        completed = workflows_domain_crud.get_completed_tasks_for_run(db, run_id)
        for t in completed:
            try:
                _check_and_dispatch_downstream(db, t)
            except Exception:  # noqa: BLE001
                pass
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("step re-kick failed for run %s: %s", run_id, e)

    return result
