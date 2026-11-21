# API/routers/workflow_triggers.py
"""Workflow Trigger CRUD endpoints.

Supported types:
  - schedule          — cron-based (existing behavior, unchanged firing path).
  - workflow_completion — fires when another workflow's run hits a terminal status.
  - webhook           — public POST URL with bcrypt-hashed token. Plaintext is
                        returned exactly once on create (and on rotate-secret).
  - file_event        — MinIO bucket-notification trigger.
  - log_pattern_alert — API log regex match.
  - queue_empty       — task-queue drain trigger.

Per-type configuration shape is enforced by `schemas.WorkflowTriggerCreate`'s
model_validator. The router adds cross-row checks that the schema can't do
without DB access (same-tenant source_workflow_id, bucket-vs-tenant match).

Gate 2b service/CRUD migration: this module is now HTTP-only (parse/validate
input, shape the response). Data access lives in `crud/workflow_triggers.py`;
business logic (per-type validation, secret minting, audit, snapshot refresh)
lives in `services/workflow_triggers_service.py`. `_strip_secret_keys` /
`_SECRET_KEYS` are re-exported here (moved to the service module) because a
test patches/imports them from this module's namespace.
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

import schemas
import models
from dependencies import get_db, require_active_and_onboarded_user, require_automation_manage_key
from services import workflow_triggers_service
from services._base import ServiceError
from services.workflow_triggers_service import _SECRET_KEYS, strip_secret_keys as _strip_secret_keys
from services import trigger_health as trigger_health_svc

# sz-4.8 per-trigger health telemetry ships in CE (owner 11-05: it is read-only
# math over data CE already collects — events, runs, dead letters, audit drops —
# and it is the only surface that reports a broken webhook at all, so leaving it
# paid would hide the failure funnel from free users). Direct import, no
# optional_paid gate, no 402 branch: the routes answer the same shapes in both
# editions, with the same tenant scoping (a foreign tenant's trigger 404s).

from product_names import themed_logger

logger = themed_logger(__name__, 'triggers')

router = APIRouter(
    prefix="/road/triggers",
    tags=["The Road", "Workflow Triggers"],
    dependencies=[Depends(require_active_and_onboarded_user)],
)


class TriggerEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: Optional[Any] = None
    event_key: Optional[str] = None
    fired_at: Optional[str] = None
    dedupe_until: Optional[str] = None
    state: Optional[str] = None


class TriggerTestResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: Optional[str] = None
    would_dispatch: Optional[bool] = None
    dispatched: Optional[bool] = None
    reason: Optional[str] = None
    event_key: Optional[Any] = None
    run_id: Optional[Any] = None


class TriggerEventsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    trigger_id: Optional[int] = None
    events: Optional[List[TriggerEvent]] = None


class TriggerHealthResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    trigger_id: Optional[int] = None
    trigger_type: Optional[str] = None
    enabled: Optional[bool] = None
    connection_status: Optional[str] = None
    last_event_at: Optional[str] = None
    events_in_window: Optional[int] = None
    runs_in_window: Optional[int] = None
    run_success_rate: Optional[float] = None
    dead_letters_in_window: Optional[int] = None
    rate_limited_in_window: Optional[int] = None


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    ts: Optional[str] = None
    source: Optional[str] = None
    level: Optional[str] = None
    message: Optional[str] = None
    metadata: Optional[Any] = None


class WorkerTask(BaseModel):
    model_config = ConfigDict(extra="allow")
    task_id: Optional[Any] = None
    worker_name: Optional[str] = None
    processing_image: Optional[str] = None
    input_hash: Optional[str] = None
    input_hashes: Optional[List[str]] = None
    output_hashes: Optional[List[str]] = None
    phases: Optional[Dict[str, Any]] = None
    phase_durations: Optional[Dict[str, Any]] = None
    outputs: Optional[List[Any]] = None
    log_tail: Optional[str] = None
    stderr_tail: Optional[str] = None
    error_message: Optional[str] = None


class RunNode(BaseModel):
    model_config = ConfigDict(extra="allow")
    node_id: Optional[str] = None
    name: Optional[str] = None
    node_kind: Optional[str] = None
    status: Optional[str] = None
    type: Optional[str] = None
    duration_s: Optional[float] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    worker_task: Optional[WorkerTask] = None


class RunLogResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    workflow_run_id: Optional[int] = None
    workflow_name: Optional[str] = None
    version: Optional[Any] = None
    status: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    total_duration_s: Optional[float] = None
    nodes: Optional[List[RunNode]] = None
    wait_times: Optional[List[Dict[str, Any]]] = None
    pass_count: Optional[int] = None
    fail_count: Optional[int] = None
    skipped_count: Optional[int] = None
    total_nodes: Optional[int] = None
    run_events: Optional[List[RunEvent]] = None


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer `ServiceError` to the exact `HTTPException` this
    router raised directly before the Gate 2b migration (same status code +
    detail shape — a plain string OR a structured dict, whichever the
    original branch raised)."""
    if exc.code == "not_found":
        raise HTTPException(status_code=404, detail=exc.message)
    if exc.code == "unprocessable_entity":
        raise HTTPException(status_code=422, detail=exc.details)
    if exc.code == "bad_request":
        raise HTTPException(status_code=400, detail=exc.message)
    raise HTTPException(status_code=400, detail=exc.message)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/",
    response_model=schemas.WorkflowTriggerCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_trigger(
    trigger_in: schemas.WorkflowTriggerCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Create a workflow trigger.

    Returns `WorkflowTriggerCreateResponse`; for `webhook` triggers,
    `webhook_secret` (plaintext) and `webhook_url` are populated. They are
    returned exactly once and never reappear on subsequent reads.
    """
    try:
        return workflow_triggers_service.create_trigger(db, trigger_in, current_user)
    except ServiceError as exc:
        _raise_http(exc)


@router.get("/", response_model=List[schemas.WorkflowTriggerResponse])
def list_triggers(
    workflow_id: int = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """List workflow triggers for this tenant. Optionally filter by workflow_id."""
    try:
        return workflow_triggers_service.list_triggers(db, current_user, workflow_id=workflow_id)
    except ServiceError as exc:
        _raise_http(exc)


@router.get("/{trigger_id}", response_model=schemas.WorkflowTriggerResponse)
def get_trigger(
    trigger_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    try:
        return workflow_triggers_service.get_trigger(db, trigger_id, current_user)
    except ServiceError as exc:
        _raise_http(exc)


class _TriggerTestRequest(BaseModel):
    event_ctx: Optional[Dict[str, Any]] = None
    mode: str = "dry_run"   # dry_run | fire


@router.post("/{trigger_id}/test", response_model=TriggerTestResponse)
def test_trigger(
    trigger_id: int,
    body: _TriggerTestRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """add-workflow-triggers §9.2 — run a synthetic event through a trigger.
    `dry_run` (default) reports whether it WOULD fire (conditions/dedup/rate-limit)
    without dispatching; `fire` actually dispatches (tenant-scoped)."""
    try:
        return workflow_triggers_service.run_trigger_test(
            db, trigger_id, current_user,
            event_ctx=body.event_ctx, mode=body.mode,
        )
    except ServiceError as exc:
        _raise_http(exc)


@router.patch("/{trigger_id}", response_model=schemas.WorkflowTriggerResponse)
def update_trigger(
    trigger_id: int,
    trigger_update: schemas.WorkflowTriggerUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Update a trigger's configuration or active flag.

    `trigger_type` cannot be changed. `configuration.secret_hash` is server-
    managed; client-supplied values are stripped before persisting. For
    webhook triggers, the existing `secret_hash` is preserved so
    configuration replacement doesn't accidentally invalidate the webhook URL.
    """
    try:
        return workflow_triggers_service.update_trigger(db, trigger_id, trigger_update, current_user)
    except ServiceError as exc:
        _raise_http(exc)


@router.delete("/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_trigger(
    trigger_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Soft-delete a trigger."""
    try:
        return workflow_triggers_service.delete_trigger(db, trigger_id, current_user)
    except ServiceError as exc:
        _raise_http(exc)


# ---------------------------------------------------------------------------
# Rotate-secret — exposed at /workflows/{workflow_id}/triggers/{trigger_id}/rotate-secret
# (a separate router because that path doesn't share the /workflows/triggers
# prefix the rest of the CRUD lives under).
# ---------------------------------------------------------------------------

nested_router = APIRouter(
    tags=["Workflow Triggers", "The Road"],
    dependencies=[Depends(require_active_and_onboarded_user)],
)


@nested_router.post(
    "/road/{workflow_id}/triggers/{trigger_id}/rotate-secret",
    response_model=schemas.WorkflowTriggerCreateResponse,
)
def rotate_webhook_secret(
    workflow_id: int,
    trigger_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Rotate a webhook trigger's secret. Returns plaintext + URL exactly once."""
    try:
        return workflow_triggers_service.rotate_webhook_secret(db, workflow_id, trigger_id, current_user)
    except ServiceError as exc:
        _raise_http(exc)


# ============================================================================
# MCP tool fleet — /v1/automations/* (automation:manage scope)
# ----------------------------------------------------------------------------
# The Automations tool: full trigger lifecycle (create/list/get/update/delete/
# rotate-secret across every trigger type) PLUS read-only observability —
# a trigger's event-arrival history and a workflow run's detailed log — so an
# agent can wire workflows to events and watch them work. Every op is
# tenant-scoped (delegates to the tenant-scoped handlers / the service's
# get_trigger_or_404). The raw platform API-log stream is deliberately NOT
# exposed here.
automations_router = APIRouter(
    prefix="/automations",
    tags=["Automations (MCP)"],
)


# --- trigger lifecycle (delegates to the CRUD handlers above) ---------------

@automations_router.post("/triggers", response_model=schemas.WorkflowTriggerCreateResponse,
                         status_code=status.HTTP_201_CREATED)
def am_create_trigger(trigger_in: schemas.WorkflowTriggerCreate,
                      db: Session = Depends(get_db),
                      user: models.User = Depends(require_automation_manage_key)):
    """Create a trigger of any type for a workflow the caller owns."""
    return create_trigger(trigger_in=trigger_in, db=db, current_user=user)


@automations_router.get("/triggers", response_model=List[schemas.WorkflowTriggerResponse])
def am_list_triggers(workflow_id: int = None,
                     db: Session = Depends(get_db),
                     user: models.User = Depends(require_automation_manage_key)):
    """List the caller tenant's triggers (optionally filtered by workflow_id)."""
    return list_triggers(workflow_id=workflow_id, db=db, current_user=user)


# --- observability (read-only, tenant-scoped) -------------------------------

@automations_router.get("/triggers/health",
                        response_model=List[TriggerHealthResponse])
def am_trigger_health_list(
    window_hours: int = Query(24, ge=1, le=720),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_automation_manage_key),
):
    """Health summary for every live trigger of the caller's tenant."""
    return trigger_health_svc.tenant_trigger_health(
        db, user.tenant_id, window_hours=window_hours,
    )


@automations_router.get("/triggers/{trigger_id}/health",
                        response_model=TriggerHealthResponse)
def am_trigger_health(
    trigger_id: int,
    window_hours: int = Query(24, ge=1, le=720),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_automation_manage_key),
):
    """Health summary for one of the caller's triggers (tenant-scoped)."""
    try:
        return trigger_health_svc.trigger_health(
            db, user.tenant_id, trigger_id, window_hours=window_hours,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Trigger not found")


@automations_router.get("/triggers/{trigger_id}", response_model=schemas.WorkflowTriggerResponse)
def am_get_trigger(trigger_id: int,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(require_automation_manage_key)):
    """Get one of the caller's triggers (404 for a foreign trigger)."""
    return get_trigger(trigger_id=trigger_id, db=db, current_user=user)


@automations_router.patch("/triggers/{trigger_id}", response_model=schemas.WorkflowTriggerResponse)
def am_update_trigger(trigger_id: int,
                      trigger_update: schemas.WorkflowTriggerUpdate,
                      db: Session = Depends(get_db),
                      user: models.User = Depends(require_automation_manage_key)):
    """Update a trigger's configuration or active flag."""
    return update_trigger(trigger_id=trigger_id, trigger_update=trigger_update,
                          db=db, current_user=user)


@automations_router.delete("/triggers/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def am_delete_trigger(trigger_id: int,
                      db: Session = Depends(get_db),
                      user: models.User = Depends(require_automation_manage_key)):
    """Soft-delete one of the caller's triggers."""
    return delete_trigger(trigger_id=trigger_id, db=db, current_user=user)


@automations_router.post("/triggers/{trigger_id}/rotate-secret",
                         response_model=schemas.WorkflowTriggerCreateResponse)
def am_rotate_secret(trigger_id: int,
                     db: Session = Depends(get_db),
                     user: models.User = Depends(require_automation_manage_key)):
    """Rotate a webhook/form trigger's secret. Plaintext + URL returned once."""
    try:
        trig = workflow_triggers_service.get_trigger_or_404(db, trigger_id, user.tenant_id)
    except ServiceError as exc:
        _raise_http(exc)
        return
    return rotate_webhook_secret(workflow_id=trig.workflow_id, trigger_id=trigger_id,
                                 db=db, current_user=user)


@automations_router.get("/triggers/{trigger_id}/events", response_model=TriggerEventsResponse)
def am_trigger_events(trigger_id: int,
                      limit: int = 50,
                      db: Session = Depends(get_db),
                      user: models.User = Depends(require_automation_manage_key)):
    """Recent event-arrival history for one of the caller's triggers (which
    events arrived, when, and dedup/debounce state). Tenant-scoped."""
    try:
        return workflow_triggers_service.trigger_events(db, trigger_id, user.tenant_id, limit=limit)
    except ServiceError as exc:
        _raise_http(exc)


@automations_router.get("/runs/{run_id}/log", response_model=RunLogResponse)
def am_run_log(run_id: int,
               db: Session = Depends(get_db),
               user: models.User = Depends(require_automation_manage_key)):
    """Detailed per-node execution log for a run in the caller's tenant."""
    from routers.workflows import get_workflow_detailed_log
    return get_workflow_detailed_log(run_id=run_id, db=db, current_user=user)
