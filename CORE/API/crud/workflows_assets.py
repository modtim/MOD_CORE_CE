"""Workflows-assets CRUD (Gate 2b, workflows_domains/assets_cogs_files.py migration).

The ONLY place `db.query(models.X)` lives for the inline query sites found in
`routers/workflows_domains/assets_cogs_files.py`'s handler bodies. Named
`workflows_assets` (not `assets_cogs_files`) to avoid colliding with the
pre-existing, unrelated `crud/assets_cogs_files.py` (that one backs
`routers/cogs/assets_cogs_files.py` — a different router package that happens
to share a basename).

Pure data access: no auth/policy decisions, no response shaping, no HTTP.
Every function here is a verbatim extraction of an inline query that used to
live in the router — filters/ordering/pagination are preserved exactly so
behavior is unchanged.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session, aliased, joinedload, selectinload

import models


# =============================================================================
# Workflow runs — GET /road/runs (list_all_workflow_runs)
# =============================================================================

def list_workflow_runs_page(
    db: Session,
    *,
    tenant_id: int,
    workflow_ids: Optional[Sequence[int]],
    status_filter: Optional[str],
    start_date: Optional[str],
    q: Optional[str],
    limit: int,
    offset: int,
    include_details: bool = True,
    purposes_exclude: Optional[Sequence[str]] = None,
) -> List[models.WorkflowRun]:
    """Tenant-scoped, optionally workflow/status/start_date-filtered page of
    WorkflowRuns, eager-loading everything the router's response-shaping loop
    touches. `workflow_ids=None` means "no per-workflow restriction" (the
    caller's view-scope was `see_all`); an empty/non-None sequence restricts to
    those workflow ids exactly as the original inline query did.

    Returns up to `limit + 1` rows (the router's original "peek ahead by one"
    trick), ordered most-recent-first (NULLs first, matching the original
    `.desc().nullsfirst()`).

    BUG.B28a — `q` (trimmed, non-empty) restricts the page to runs whose
    workflow name, triggering user (first/last name, username, email), status
    value or run id match. The filter runs BEFORE ordering/pagination, so both
    the page and the caller's total count span the filtered set.

    `run_files` and `node_runs` are loaded via `selectinload` (separate IN()
    queries), NOT `joinedload`. Two collection joinedloads in one statement
    is a cartesian product (run_files x node_runs rows per run) wrapped in a
    LIMIT+OFFSET subquery — the same pattern already fixed on the per-workflow
    `/road/{id}/runs` endpoint (runs-list-cartesian-and-idle-txn-500).
    This endpoint (the all-tenant `/road/runs` list backing the
    Dashboard's "quick stats" tiles and the Logs page's 500-row background
    poll) still joinedload'd both collections here, so a 500-row page with
    even a modest fan-out of files/node-runs per run made the DB stream
    `rows_per_run = len(run_files) * len(node_runs)` duplicate-heavy rows for
    every run in the page, multiplying with `limit`. selectinload issues one
    extra `WHERE workflow_run_id IN (...)` query per collection instead —
    linear in `limit`, no duplication — and the response shape is unchanged."""
    options = [
        joinedload(models.WorkflowRun.workflow),          # to-one: safe to join
        joinedload(models.WorkflowRun.workflow_version),  # to-one: safe to join
    ]
    if include_details:
        options += [
            selectinload(models.WorkflowRun.run_files),
            selectinload(models.WorkflowRun.node_runs).joinedload(models.NodeRun.workflow_node),
        ]
    query = db.query(models.WorkflowRun).options(*options)

    # Tenant-scoped for everyone (ISO/DoD tenant isolation): no admin widening.
    query = query.filter(models.WorkflowRun.tenant_id == tenant_id)

    if purposes_exclude:
        query = query.filter(
            models.WorkflowRun.purpose.notin_(purposes_exclude)
        )

    if workflow_ids is not None:
        query = query.filter(models.WorkflowRun.workflow_id.in_(workflow_ids))

    # Optional status filter (e.g., "running", "queued", "completed", "failed")
    if status_filter:
        status_map = {
            "running": models.TaskStatus.RUNNING,
            "queued": models.TaskStatus.QUEUED,
            "completed": models.TaskStatus.COMPLETED,
            "failed": models.TaskStatus.FAILED,
        }
        if status_filter.lower() in status_map:
            query = query.filter(models.WorkflowRun.status == status_map[status_filter.lower()])

    # Optional start-date filter (ISO 8601, e.g. the dashboard's
    # "Last 30 Days"). Applied server-side BEFORE pagination so pages span the
    # full filtered set rather than a single pre-fetched window.
    if start_date:
        try:
            # JS toISOString() emits trailing 'Z'; started_at is stored
            # naive-UTC, so drop tzinfo to keep the comparison naive.
            sd = datetime.fromisoformat(start_date.replace("Z", "+00:00")).replace(tzinfo=None)
            query = query.filter(models.WorkflowRun.started_at >= sd)
        except ValueError:
            pass

    # BUG.B28a — free-text search ("Logs → Workflow runs: no search"). One
    # statement, no N+1: both sides are correlated OUTER joins on aliased
    # classes (workflow_id / triggered_by_user_id are to-one, so neither
    # multiplies rows and a scheduled run with no user still matches on
    # name/status/id). A bare `models.Workflow.name` filter would NOT reuse
    # the joinedload above — SQLAlchemy adds a second, UNCORRELATED join
    # (`FROM workflow_runs, workflows`), which matches every run in the
    # tenant against ANY workflow name. LIKE wildcards in q are escaped so
    # `%` and `_` stay literal. Applied BEFORE ordering/pagination, so the
    # page and its `limit + 1` peek span the filtered set (the router's
    # total is derived from the page, so it counts the filtered set too).
    needle = (q or "").strip()
    if needle:
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        actor = aliased(models.User)
        wf_alias = aliased(models.Workflow)
        status_hits = [st for st in models.TaskStatus if st.value.lower() == needle.lower()]
        clauses = [
            wf_alias.name.ilike(like),
            actor.first_name.ilike(like),
            actor.last_name.ilike(like),
            actor.username.ilike(like),
            actor.email.ilike(like),
        ]
        if needle.isdigit():
            clauses.append(models.WorkflowRun.id == int(needle))
        clauses += [models.WorkflowRun.status == st for st in status_hits]
        query = (
            query.outerjoin(
                wf_alias, models.WorkflowRun.workflow_id == wf_alias.id
            ).outerjoin(
                actor, models.WorkflowRun.triggered_by_user_id == actor.id
            ).filter(or_(*clauses))
        )

    query = query.order_by(models.WorkflowRun.started_at.desc().nullsfirst())

    return query.offset(offset).limit(limit + 1).all()


# =============================================================================
# Workflow access requests (request_workflow_access)
# =============================================================================

def get_pending_workflow_access_request(
    db: Session, requester_id: int, workflow_id: int
) -> Optional[models.PermissionRequest]:
    """A requester's still-pending access request against a given workflow, if
    any (dup check)."""
    return db.query(models.PermissionRequest).filter(
        models.PermissionRequest.requester_id == requester_id,
        models.PermissionRequest.resource_type == "workflow",
        models.PermissionRequest.resource_id == workflow_id,
        models.PermissionRequest.status == models.PermissionRequestStatus.PENDING,
    ).first()


# =============================================================================
# Queue status (get_queue_status)
# =============================================================================

def count_queue_pending(db: Session, queue_name: str) -> int:
    from models import ApiTaskQueue, ApiTaskStatus

    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.queue_name == queue_name,
        ApiTaskQueue.status.in_([ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED]),
    ).count()


def count_queue_running(db: Session, queue_name: str) -> int:
    from models import ApiTaskQueue, ApiTaskStatus

    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.queue_name == queue_name,
        ApiTaskQueue.status.in_([ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING]),
    ).count()


def count_licensed_workers(db: Session) -> int:
    from models import WorkerLicense, WorkerStatus

    return db.query(WorkerLicense).filter(
        WorkerLicense.status == WorkerStatus.LICENSED,
        WorkerLicense.deleted_at.is_(None),
    ).count()


def get_confident_resource_patterns(db: Session, queue_name: str) -> List["models.ResourcePattern"]:
    from models import ResourcePattern

    return db.query(ResourcePattern).filter(
        ResourcePattern.queue_name == queue_name,
        ResourcePattern.confidence > 0.3,
    ).all()


# =============================================================================
# Worker/service token exchange (exchange_workflow_token)
# =============================================================================

def get_workflow_run_untenant(db: Session, workflow_run_id: int) -> Optional[models.WorkflowRun]:
    """Fetch a WorkflowRun by id, tenant-UNSCOPED — matches the original
    inline query exactly. This endpoint authenticates via service API
    key/bearer (worker containers), not a tenant-bound User, so there is no
    caller tenant to scope by; the run's own `tenant_id` is read off the
    returned row afterward."""
    return db.query(models.WorkflowRun).filter(
        models.WorkflowRun.id == workflow_run_id
    ).first()


def get_tenant_by_id(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
