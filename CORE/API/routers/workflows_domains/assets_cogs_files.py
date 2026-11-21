# Auto-generated domain sub-module (Gate 2a split of routers/workflows.py).
# Behavior-preserving: endpoints MOVED verbatim, grouped by audit domain.
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, status, HTTPException, BackgroundTasks, Body, UploadFile, File, Query
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
from product_names import themed_logger
logger = themed_logger('routers.workflows_domains.assets_cogs_files', 'workflows')
from pydantic import BaseModel
import json
import io
import re
import zipfile
from dependencies import require_bearer_or_api_key
from utils.run_task_auth import node_or_onboarded_user_principal, pin_run_task_tenant
from utils.tenant_lifecycle import require_tenant_writable

from routers.workflows_domains._shared import *

from crud import workflows_assets as workflows_assets_crud
from services import workflows_assets_service

router = APIRouter()
service_router = APIRouter()


# =============================================================================
# BUG.B27 — Dashboard "Logs → Workflow runs" row enrichment helpers
# =============================================================================
_TRIGGER_LABELS = {
    "schedule": "Schedule",
    "webhook": "Webhook",
    "workflow_completion": "Workflow",
    "file_event": "File event",
    "log_pattern_alert": "Log alert",
    "queue_empty": "Queue empty",
    "interval": "Schedule",
    "one_shot": "Schedule",
    "node_completion": "Workflow",
    "fan_in": "Workflow",
    "system_threshold": "System",
    "db_row_event": "System",
    "api_call": "API call",
    "email": "Webhook",
    "chatops": "Webhook",
    "git": "Webhook",
    "manual": "Manual",
    "approval": "Manual",
    "form": "Webhook",
}


def _run_actor_labels(db, page_runs):
    """({user_id: display}, {trigger_id: label}) for a page of runs.

    Two batched IN() queries for the whole page — never one per row (the
    Dashboard polls this endpoint at limit=500). Display name is
    first/last name when present, else username, else email (models.User
    carries no dedicated display-name column).

    BUG.B27b — the same two queries also carry the raw username per user id
    (third map), so the runs list can show "Tenant ID / User ID / username"
    next to the human display name without a third query.
    """
    user_ids = sorted({r.triggered_by_user_id for r in page_runs
                       if r.triggered_by_user_id})
    trig_ids = sorted({r.triggered_by_trigger_id for r in page_runs
                       if r.triggered_by_trigger_id})
    actor_names, actor_usernames = {}, {}
    if user_ids:
        for u in db.query(models.User).filter(
                models.User.id.in_(user_ids)).all():
            first, last = (u.first_name or "").strip(), (u.last_name or "").strip()
            full = f"{first} {last}".strip()
            actor_names[u.id] = full or u.username or u.email or f"User #{u.id}"
            # BUG.B27b — the raw handle too (same query, no extra round-trip).
            actor_usernames[u.id] = u.username
    trigger_kinds = {}
    if trig_ids:
        for tr in db.query(models.WorkflowTrigger).filter(
                models.WorkflowTrigger.id.in_(trig_ids)).all():
            trigger_kinds[tr.id] = tr.trigger_type.value if tr.trigger_type else None
    return actor_names, actor_usernames, trigger_kinds


def _run_trigger_label(kind):
    """Human label for a trigger kind ('schedule' -> 'Schedule')."""
    return _TRIGGER_LABELS.get(kind, kind) if kind else None


def _run_actor_label(run, actor_names, trigger_kinds):
    """One human string for "who ran it": the user, else the trigger kind,
    else System (runs dispatched by the platform's own service principal)."""
    if run.triggered_by_user_id:
        return actor_names.get(run.triggered_by_user_id) or f"User #{run.triggered_by_user_id}"
    if run.triggered_by_trigger_id:
        return _run_trigger_label(trigger_kinds.get(run.triggered_by_trigger_id)) or "Trigger"
    return "System"


def _run_outcome_summary(run):
    """"3/3 nodes ok" or the first node failure reason (truncated).

    Derived from the eager-loaded node_runs / error_message — no extra query.
    """
    node_runs = run.node_runs or []
    failed = [nr for nr in node_runs
              if nr.status and nr.status.value in ("failed", "failure", "blocked")]
    ok = len(node_runs) - len(failed)
    if run.error_message:
        return str(run.error_message)[:160]
    if failed:
        reason = (failed[0].error_message or "node failed")
        return f"{len(failed)} node(s) failed: {str(reason)[:120]}"
    if node_runs:
        return f"{ok}/{len(node_runs)} nodes ok"
    return None


@router.get("/runs", response_model=List[schemas.WorkflowRun])
def list_all_workflow_runs(
        limit: int = 10,
        offset: int = 0,
        status_filter: Optional[str] = None,
        start_date: Optional[str] = None,
        q: Optional[str] = Query(None, max_length=200),
        include_details: bool = Query(
            True,
            description="BUG.B48 — set false for count-only callers (the "
                        "Dashboard Home Quick-stats tiles): skips the node_runs/"
                        "run_files eager loads and per-row detail shaping. The "
                        "rows still carry id/status/timestamps, which is all a "
                        "count needs.",
        ),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Lists all workflow runs for the current user's tenant.
    Returns runs with workflow name, version, and file information.
    Used by Queue dialogs and dashboards.

    NOTE: This route MUST be defined before /{workflow_id} routes to avoid
    FastAPI matching "/runs" as a workflow_id parameter.

    BUG.B28a — `q` is the Dashboard "Logs → Workflow runs" search box: a
    case-insensitive match on workflow name, the triggering user's name/
    username/email, the run's status, or (numeric q) the run id. Filtering
    happens inside the single page query, so pagination and the row set stay
    consistent.
    """
    # Per-workflow visibility: a standard user sees run history only for
    # workflows in their view scope (owned or granted), mirroring GET
    # /workflows/{id}. Without this, tenant-scoping alone leaked run metadata
    # (and costs/outputs) for the owner's private workflows. Platform admins
    # retain full visibility (system/admin path preserved).
    see_all, visible_ids = _run_view_scope(db, current_user)
    if not see_all and not visible_ids:
        return []

    runs = workflows_assets_crud.list_workflow_runs_page(
        db,
        tenant_id=current_user.tenant_id,
        workflow_ids=None if see_all else visible_ids,
        status_filter=status_filter,
        start_date=start_date,
        q=q,
        limit=limit,
        offset=offset,
        include_details=include_details,
        purposes_exclude=("adversary_dispatch",),
    )

    # GY.C7 — batched effective data-classification labels for the runs
    # shown (highest label among each run's input cogs). Fail-OPEN: a
    # lookup hiccup must not 500 the Dashboard runs list — it just hides
    # the chip.
    try:
        from services import data_classification as _gy_c7_dc
        _gy_c7_labels = _gy_c7_dc.run_labels_for_runs(db, runs[:limit])
    except Exception:
        _gy_c7_labels = {}

    # BUG.B27 — who caused each run, resolved in TWO batch queries (one IN()
    # over the page's distinct user ids, one over the page's distinct trigger
    # ids), never per row. SQLAlchemy here is 2.0.x: `selectin` (FK-column
    # eager load) does not exist, and a joinedload would need a relationship
    # on models.WorkflowRun that does not exist — so batching is the only
    # N+1-free shape. Tenant scoping is untouched: the run page itself is
    # still scoped+gated by list_workflow_runs_page/_run_view_scope below.
    page_runs = runs[:limit]
    actor_names, actor_usernames, trigger_kinds = _run_actor_labels(db, page_runs)

    # BUG.B27b — tenant name for the page's tenants, in ONE batched IN() query
    # (the page is tenant-scoped, so this is normally a single row; batching
    # keeps it N+1-free if a caller ever widens the page).
    tenant_names = {}
    tenant_ids = sorted({r.tenant_id for r in page_runs if r.tenant_id})
    if tenant_ids:
        for t in db.query(models.Tenant).filter(
                models.Tenant.id.in_(tenant_ids)).all():
            tenant_names[t.id] = t.name

    # Enrich with workflow name and version number from relationships
    result = []
    for run in page_runs:
        run_dict = {
            "id": run.id,
            "run_name": run.run_name,
            "status": run.status.value if run.status else "queued",
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "workflow_id": run.workflow_id,
            "workflow_version_id": run.workflow_version_id,
            "workflow_name": run.workflow.name if run.workflow else None,
            "version_number": run.workflow_version.version_number if run.workflow_version else None,
            "orchestrator_run_id": run.orchestrator_run_id,
            # GY.C7 — effective data-classification label (see batch above).
            "data_classification": _gy_c7_labels.get(run.id),
            # BUG.B27 — "who ran it" + "what happened", so the Dashboard
            # runs list is findable without opening each run.
            "triggered_by": _run_actor_label(run, actor_names, trigger_kinds),
            "triggered_by_user_id": run.triggered_by_user_id,
            "trigger_type": (trigger_kinds.get(run.triggered_by_trigger_id)
                             if run.triggered_by_trigger_id else None),
            # BUG.B27b — a username alone cannot identify who ran a run when
            # the owner has hundreds of users across tenants, so the row also
            # carries the tenant id/name and the numeric user id + raw handle.
            "tenant_id": run.tenant_id,
            "tenant_name": tenant_names.get(run.tenant_id),
            "triggered_by_username": (actor_usernames.get(run.triggered_by_user_id)
                                      if run.triggered_by_user_id else None),
            # BUG.B48 — with include_details=false the collections were never
            # eager-loaded, so they must not be touched here: a lazy FK access
            # per row would be an N+1. The count-only tiles read none of these.
            "outcome": (_run_outcome_summary(run) if include_details else None),
            "node_runs": ([
                {
                    "id": nr.id,
                    "workflow_node_id": nr.workflow_node_id,
                    "status": nr.status.value if nr.status else "queued",
                    "started_at": nr.started_at,
                    "completed_at": nr.completed_at,
                    "node_id": nr.workflow_node.graph_node_id if nr.workflow_node else None
                }
                for nr in run.node_runs
            ] if include_details else []),
            "run_files": ([
                {
                    "id": rf.id,
                    "file_id": rf.file_id,
                    "title": rf.title,
                    "status": rf.status.value if rf.status else None
                }
                for rf in run.run_files
            ] if include_details else [])
        }
        result.append(run_dict)

    return result

@router.get("/{workflow_id:int}", response_model=schemas.Workflow)
def get_workflow(
        workflow_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Retrieves details for a single workflow.
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "view"):
        # It exists in your tenant but you have no grant — 403 with a structured
        # payload so the Workflow Builder can show a "request access" prompt
        # (cross-tenant stays 404 above via the tenant filter).
        raise HTTPException(status_code=403, detail={
            "code": "workflow_access_denied",
            "message": "You don't have access to this workflow.",
            "workflow_id": db_workflow.id,
            "workflow_name": db_workflow.name,
        })
    # GSEC FX.79 p4a — reading a data-marked workflow is audit-logged
    # (no-op when the workflow carries no live mark; never raises).
    from utils.paid_import import optional_paid
    _dm_p4a = optional_paid("services.data_markings")
    # GSEC FX.79 p4b — an ITAR-class mark restricts this read to confirmed
    # US persons (the admin-set attribute, GY.V2b). Checked BEFORE the read
    # is audit-logged/served: a refusal writes its own denial row through
    # the p4a record path (inside the gate) and never emits the read row.
    if _dm_p4a is not None:  # FX.162.p5 — CE carve: no markings -> no US gate, no audit stamp
        try:
            _dm_p4a.assert_us_person_access(
                db, current_user.tenant_id, "workflow", workflow_id,
                user_id=current_user.id, action="read")
        except _dm_p4a.DataMarkingBlockedError as _dm_us_exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "detail": _dm_us_exc.message,
                    "code": _dm_us_exc.code,
                    "labels": (_dm_us_exc.details or {}).get("labels"),
                    "action": "workflow_read",
                },
                headers={"X-Error-Code": _dm_us_exc.code},
            )
        _dm_p4a.record_marked_access(
            db, tenant_id=current_user.tenant_id, user_id=current_user.id,
            resource_type="workflow", resource_id=workflow_id, action="read")
    # GY.C7 — workflow's effective label (highest label among its most
    # recent run's input cogs). Set on the transient instance for the
    # response (the Workflow ORM row carries no such column); fail-OPEN —
    # a lookup hiccup hides the marking, never 500s the load.
    try:
        from services import data_classification as _gy_c7_dc
        db_workflow.data_classification = _gy_c7_dc.workflow_label(
            db, db_workflow.id, tenant_id=current_user.tenant_id)
    except Exception:
        db_workflow.data_classification = None
    return db_workflow

@router.post("/{workflow_id:int}/request-access", status_code=status.HTTP_201_CREATED)
def request_workflow_access(
        workflow_id: int,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
        justification: Optional[str] = Body(None, embed=True),
):
    """Create a pending access request for a workflow the caller can't use.
    The workflow owner (or a platform admin) reviews it in the Permission
    Requests inbox; approving grants the requester 'view'."""
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if _user_can_access_workflow(db, current_user, db_workflow, "view"):
        raise HTTPException(status_code=400, detail="You already have access to this workflow")
    try:
        req, owner = workflows_assets_service.create_workflow_access_request(
            db, current_user, db_workflow, justification
        )
    except workflows_assets_service.DuplicateAccessRequestError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Notify the workflow owner by email (best-effort; mirrors the cog request
    # flow). The owner also sees the request in their Permission Requests inbox.
    if owner and owner.email and background_tasks is not None:
        try:
            notification_conf = {
                "notification_type": "new_request",
                "recipient_email": owner.email,
                "recipient_name": f"{owner.first_name} {owner.last_name}",
                "requester_name": f"{current_user.first_name} {current_user.last_name}",
                "requester_email": current_user.email,
                "resource_type": "workflow",
                "resource_name": db_workflow.name,
                "folder_name": db_workflow.name,  # template key reused for the resource name
                "requested_level": "viewer",
                "duration": "Permanent",
                "requested_date": req.created_at.strftime("%Y-%m-%d"),
                "justification": justification,
                "dashboard_url": f"{os.getenv('DASHBOARD_URL', 'http://dashboard.localhost')}/#permission-requests",
                "tenant_id": current_user.tenant_id,
            }
            from utils.email_sender import send_permission_request_email
            background_tasks.add_task(
                send_permission_request_email,
                db=db,
                tenant_id=current_user.tenant_id,
                recipient_email=owner.email,
                recipient_name=f"{owner.first_name} {owner.last_name}",
                request_details=notification_conf,
            )
        except Exception:
            logging.warning("Failed to queue workflow access-request email", exc_info=True)

    return {"detail": "Access request submitted", "request_id": req.id, "workflow_name": db_workflow.name}

@router.patch("/{workflow_id}", response_model=schemas.Workflow,
              dependencies=[Depends(require_tenant_writable)])
def update_workflow(
        workflow_id: int,
        workflow_update: schemas.WorkflowUpdate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Updates a workflow's top-level details, such as its name.
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "edit"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    return crud.update_workflow(db=db, db_workflow=db_workflow, workflow_update=workflow_update)

@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(
        workflow_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Soft deletes a workflow and all its associated versions and runs.
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "delete"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    # expr 9.10 — tombstone the workflow's memory rows in the SAME transaction
    # as the workflow soft-delete.  crud.delete_workflow commits internally, so
    # this must run BEFORE it: the memory UPDATEs are still uncommitted here and
    # land in that one commit.  It also has to run before the row carries
    # deleted_at so the slug lookup resolves on the live row.  Its result never
    # affects the response; it never raises.
    from services import workflow_memory_lifecycle as _wfl_mem
    try:
        _wfl_mem.soft_delete_workflow_memory(
            db, tenant_id=db_workflow.tenant_id, workflow_id=db_workflow.id)
    except Exception:
        logging.warning("workflow-memory tombstone failed for a workflow "
                        "(class-only, no values logged)")
    crud.delete_workflow(db=db, db_workflow=db_workflow)
    return

@router.post("/{workflow_id:int}/duplicate", response_model=schemas.Workflow, status_code=status.HTTP_201_CREATED)
def duplicate_workflow(
        workflow_id: int,
        new_name: Optional[str] = Body(None, embed=True),
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Duplicate a workflow: creates a new workflow container owned by the caller,
    with a first version copied (sanitized) from the source's latest version.
    Requires the 'duplicate' capability on the source workflow.
    """
    db_workflow = crud.get_workflow_by_id(db, workflow_id=workflow_id, tenant_id=current_user.tenant_id)
    if db_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _user_can_access_workflow(db, current_user, db_workflow, "duplicate"):
        raise HTTPException(status_code=404, detail="Workflow not found")
    # GG.8 — duplicating a sealed installed copy would clone the hidden graph.
    from services import sealed_workflow as _sealed
    _sealed.require_graph_readable(db, db_workflow, current_user)

    copy = crud.duplicate_workflow(
        db=db,
        source=db_workflow,
        user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        new_name=new_name,
    )

    # Grant the caller owner permissions on the new workflow (mirrors create).
    try:
        from resource_permissions import grant_owner_permissions
        grant_owner_permissions(db, current_user.tenant_id, current_user.id, models.ResourceType.WORKFLOW, copy.id)
        db.commit()
    except Exception:
        pass

    return copy

@router.get("/queue-status")
def get_queue_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    #95: Predictive queue depth — show estimated wait times per queue.

    Returns pending task counts and estimated wait times so users know
    what to expect before submitting a workflow.
    """
    queues = {}
    for queue in ["CPU", "GPU"]:
        pending = workflows_assets_crud.count_queue_pending(db, queue)

        running = workflows_assets_crud.count_queue_running(db, queue)

        # Count available workers
        workers = workflows_assets_crud.count_licensed_workers(db)

        # Estimate average task duration from patterns
        avg_pattern = workflows_assets_crud.get_confident_resource_patterns(db, queue)
        avg_duration = 120.0  # default 2 min
        if avg_pattern:
            total = sum(p.avg_duration_seconds or 120 for p in avg_pattern)
            avg_duration = total / len(avg_pattern)

        # Estimate wait: pending tasks * avg duration / max(workers, 1)
        effective_workers = max(workers, 1)
        estimated_wait_s = (pending * avg_duration) / effective_workers

        queues[queue] = {
            "pending": pending,
            "running": running,
            "workers": workers,
            "avg_task_duration_s": round(avg_duration, 1),
            "estimated_wait_s": round(estimated_wait_s, 1),
            "estimated_wait_human": _format_duration(estimated_wait_s),
        }

    return {"queues": queues}

@router.get("/workflow-versions/{version_id}/variables")
def get_workflow_variables(
        version_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get all configurable variables for a workflow version.

    Returns detailed information about what extra_variables can/should be passed
    when running this workflow via the API. This is essential for API integrations
    to know what configuration each node in the workflow accepts.

    Response includes:
    - input_requirements: What file types (cog_ids) the workflow expects
    - variables: All configurable variables across all nodes, with:
        - node_name: Which node this variable belongs to
        - variable_name: The key to use in extra_variables
        - type: Data type (string, integer, number, boolean)
        - default: Default value if not provided
        - required: Whether this must be provided
        - description: Human-readable explanation
        - enum: If present, the allowed values
        - enum_labels: Human-readable labels for enum values

    Example usage:
        GET /workflow-versions/123/variables

        Response:
        {
            "workflow_name": "Text to Image Pipeline",
            "version": 1,
            "input_requirements": {
                "accepts_files": false,
                "file_types": [],
                "description": "This workflow does not require input files"
            },
            "variables": [
                {
                    "node_name": "Text Input",
                    "node_id": "node_1",
                    "variable_name": "text_content",
                    "type": "string",
                    "default": "",
                    "required": true,
                    "description": "Enter your text prompt here"
                },
                {
                    "node_name": "Text To Image",
                    "node_id": "node_2",
                    "variable_name": "width",
                    "type": "integer",
                    "default": 512,
                    "required": false,
                    "description": "Generated image width",
                    "enum": [256, 512, 768, 1024],
                    "enum_labels": {"256": "256px (Fast)", "512": "512px (Standard)", ...}
                }
            ],
            "example_request": {
                "cog_ids": [],
                "extra_variables": {
                    "text_content": "A beautiful sunset",
                    "width": 1024
                }
            }
        }
    """
    # Get the workflow version
    db_version = crud.get_workflow_version_by_id(db, version_id=version_id, tenant_id=current_user.tenant_id)
    if db_version is None:
        raise HTTPException(status_code=404, detail="Workflow version not found")

    # AUTHORIZATION: this returns the version's node/variable content derived from
    # graph_json — a workflow-content endpoint. Gate it on the parent workflow's
    # "view" grant (same as GET /workflow-versions/{id}) so a tenant user with no
    # grant can't enumerate another user's workflow by version id.
    parent_workflow = crud.get_workflow_by_id(
        db, workflow_id=db_version.workflow_id, tenant_id=current_user.tenant_id)
    if parent_workflow is None:
        raise HTTPException(status_code=404, detail="Workflow version not found")
    if not _user_can_access_workflow(db, current_user, parent_workflow, "view"):
        raise HTTPException(status_code=403, detail={
            "code": "workflow_access_denied",
            "message": "You don't have access to this workflow.",
            "workflow_id": parent_workflow.id,
            "workflow_name": parent_workflow.name,
        })
    # GG.8 — sealed installed copies hide the graph-derived variables.
    from services import sealed_workflow as _sealed
    _sealed.require_graph_readable(db, parent_workflow, current_user)

    graph = db_version.graph_json
    if not isinstance(graph, dict) or "nodes" not in graph:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The stored workflow version has an invalid graph_json format."
        )

    nodes = graph.get("nodes", [])
    workflow_name = db_version.workflow.name if db_version.workflow else "Workflow"

    # Collect all variables from all nodes
    variables = []
    input_types = set()
    has_file_input = False

    for node in nodes:
        node_data = node.get("data", {})
        node_id = node.get("id", "unknown")
        node_name = node_data.get("label", node_data.get("name", "Unknown Node"))
        node_type = node_data.get("type", "")

        # Check input schema for file requirements
        input_schema = node_data.get("input_schema", {})
        input_type = input_schema.get("input_type", "NONE")
        if input_type and input_type != "NONE" and input_type != "text":
            has_file_input = True
            input_types.add(input_type)

        # Get configuration schema which contains extra_variables
        config_schema = node_data.get("configuration_schema", {})
        extra_vars = config_schema.get("extra_variables", {})

        # Skip if no extra_variables or it's "NONE"
        if not extra_vars or extra_vars == "NONE":
            continue

        # Handle the case where extra_variables is a dict of variable definitions
        if isinstance(extra_vars, dict):
            for var_name, var_config in extra_vars.items():
                if not isinstance(var_config, dict):
                    continue

                variable_info = {
                    "node_name": node_name,
                    "node_id": node_id,
                    "variable_name": var_name,
                    "type": var_config.get("type", "string"),
                    "default": var_config.get("default"),
                    "required": var_config.get("required", False),
                    "description": var_config.get("description", ""),
                }

                # Include enum info if present
                if "enum" in var_config:
                    variable_info["enum"] = var_config["enum"]
                if "enum_labels" in var_config:
                    variable_info["enum_labels"] = var_config["enum_labels"]

                variables.append(variable_info)

    # Build input requirements description
    if has_file_input:
        input_requirements = {
            "accepts_files": True,
            "file_types": list(input_types),
            "description": f"This workflow requires {', '.join(input_types)} file(s). Pass file IDs in the cog_ids array."
        }
    else:
        input_requirements = {
            "accepts_files": False,
            "file_types": [],
            "description": "This workflow does not require input files. Pass an empty cog_ids array."
        }

    # Build example request
    example_extra_vars = {}
    for var in variables:
        if var.get("required") or var.get("default") is None:
            # Use a placeholder for required fields or fields without defaults
            if var["type"] == "string":
                example_extra_vars[var["variable_name"]] = f"<your {var['variable_name']}>"
            elif var["type"] == "integer":
                example_extra_vars[var["variable_name"]] = var.get("default", 0)
            elif var["type"] == "number":
                example_extra_vars[var["variable_name"]] = var.get("default", 0.0)
            elif var["type"] == "boolean":
                example_extra_vars[var["variable_name"]] = var.get("default", False)
        else:
            # Optional fields with defaults - show the default
            example_extra_vars[var["variable_name"]] = var.get("default")

    example_request = {
        "cog_ids": ["<file_id_1>", "<file_id_2>"] if has_file_input else [],
        "extra_variables": example_extra_vars
    }

    return {
        "workflow_name": workflow_name,
        "version": db_version.version_number,
        "version_id": version_id,
        "input_requirements": input_requirements,
        "variables": variables,
        "example_request": example_request
    }

@service_router.post("/runs/{workflow_run_id}/exchange-token")
def exchange_workflow_token(
    workflow_run_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key)
):
    """
    Exchanges a workflow run's stored refresh token for a fresh Keycloak access token.

    This endpoint is used by worker containers to obtain user access tokens for:
    - Fetching user-specific secrets (API keys, credentials)
    - Accessing tenant-scoped resources
    - Maintaining user authentication context during long-running workflows

    Security:
    - Requires service API key authentication
    - Validates workflow_run exists and is active
    - Returns fresh access token via Keycloak token exchange
    - Full audit trail in Keycloak

    Args:
        workflow_run_id: ID of the running workflow

    Returns:
        {"access_token": "eyJ...", "expires_in": 300}
    """
    return workflows_assets_service.exchange_workflow_token(
        db, workflow_run_id, current_user
    )

@router.get("/workflow-runs/{run_id}", response_model=schemas.WorkflowRun)
def get_workflow_run_details(
        run_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Gets the status and details of a specific workflow run.
    Platform admins can see any run, others only see their tenant's runs.
    """
    # Platform admins can see all runs, others are filtered by tenant
    tenant_filter = current_user.tenant_id  # ISO: tenant-scoped, no admin widening
    db_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=tenant_filter)
    if db_run is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    # Per-workflow visibility: hide (404) a run whose workflow the user can't view.
    if not _user_can_view_run(db, current_user, db_run):
        raise HTTPException(status_code=404, detail="Workflow run not found")

    # GG.8b sealed: hide graph node identity (node_id) from the run detail
    # for sealed buyers — per-node status stays, keyed by opaque ids.
    from services import sealed_workflow as _sealed
    _detail_redact = (
        _sealed.should_redact_graph(db, db_run.workflow, current_user)
        if db_run.workflow else False
    )

    # Build response — hide timing for plans below Pro
    can_see_timing = _user_can_see_timing(db, current_user)
    return {
        "id": db_run.id,
        "run_name": db_run.run_name,
        "status": db_run.status.value if db_run.status else "queued",
        "started_at": db_run.started_at if can_see_timing else None,
        "completed_at": db_run.completed_at if can_see_timing else None,
        "timing_available": can_see_timing,
        "workflow_id": db_run.workflow_id,
        "workflow_version_id": db_run.workflow_version_id,
        "workflow_name": db_run.workflow.name if db_run.workflow else None,
        "version_number": db_run.workflow_version.version_number if db_run.workflow_version else None,
        "orchestrator_run_id": db_run.orchestrator_run_id,
        "node_runs": [
            {
                "id": nr.id,
                "workflow_node_id": nr.workflow_node_id,
                "status": nr.status.value if nr.status else "queued",
                "started_at": nr.started_at if can_see_timing else None,
                "completed_at": nr.completed_at if can_see_timing else None,
                "node_id": None if _detail_redact
                    else (nr.workflow_node.graph_node_id if nr.workflow_node else None),
                # BUG 1134 — this dict was built with no "outputs" key at all, so
                # schemas.NodeRun.outputs (declared Optional[...] = None) silently
                # defaulted to null on every run detail response, no matter what
                # was actually written to node_runs.outputs on disk (verified live:
                # DB had a real agent-node envelope, this endpoint still served
                # null). That made any poll-for-outputs contract (e.g. the 981b
                # agent-node-tool-call park handoff via sub_workflow_child_run_id)
                # structurally unobservable through this endpoint. The schema
                # field type is List[Dict[str, Any]] (worker-reported node
                # outputs are normally a list, one entry per input file), but an
                # API-side agent node's envelope (services/agent_node_runtime.py
                # _envelope) is a single dict ({"final":..., "history":...}), and
                # routers/worker_tasks.py already stores dict-shaped outputs too
                # (3290-3317) — so normalize here rather than widen the shared
                # schema (touches every other node_runs consumer).
                "outputs": (nr.outputs if isinstance(nr.outputs, list)
                            else ([nr.outputs] if isinstance(nr.outputs, dict)
                                  else None)),
            }
            for nr in db_run.node_runs
        ],
        "run_files": [
            {
                "id": rf.id,
                "file_id": rf.file_id,
                "title": rf.title,
                "status": rf.status.value if rf.status else None
            }
            for rf in db_run.run_files
        ]
    }

@router.get("/workflow-versions/{version_id}/runs", response_model=List[schemas.WorkflowRun])
def get_workflow_version_runs(
        version_id: int,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "started_at",
        order: str = "desc",
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Gets the status and details of a specific workflow run.
    """
    db_version = crud.get_workflow_version_by_id(db, version_id=version_id, tenant_id=current_user.tenant_id)
    if db_version is None:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    if not _user_can_view_workflow(db, current_user, db_version.workflow_id):
        raise HTTPException(status_code=404, detail="Workflow run not found")
    # Not a hot poll path — keep full per-node detail for the version history view.
    # WSIP 6.8: hide adversary_dispatch runs from this user-facing history too.
    return crud.get_workflow_runs(db, workflow_id=db_version.workflow_id, limit=limit, offset=offset, sort_by=sort_by,
                                  order=order, include_details=True,
                                  purposes_exclude=("adversary_dispatch",))
