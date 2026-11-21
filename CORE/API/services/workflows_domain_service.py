"""Workflows-domain service layer (Gate 2, stack-training-and-prewarm Workstream B).

Named `workflows_domain_service` (not `workflow_orchestrator`/`calibration_workflow`)
to avoid colliding with existing `services/workflow_orchestrator.py` and
`services/calibration_workflow.py`.

Holds the *policy* extracted out of `routers/workflows_domains/workflows.py`
handlers that do more than a single read — i.e. own a transaction boundary
across more than one write. Pure single-query reads stay as direct
`crud.workflows_domain` calls from the router (matching this codebase's
existing convention of routers calling `crud.*` directly for simple fetches —
see `crud.get_workflow_by_id` used throughout this same router file).

Per SHARED RULES (wave 1): this is a body-only move. Each function below is a
verbatim extraction of logic that used to live inline in the router endpoint;
control flow, log messages and commit points are preserved exactly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy.orm import Session

import crud
import models
from crud import workflows_domain as workflows_domain_crud


def get_billing_context(
    db: Session, tenant_id: int
) -> Tuple[Optional[models.Tenant], Optional[models.Subscription], Optional[models.Plan]]:
    """tenant / subscription / plan for predict_workflow_cost. `subscription`
    and `plan` are None when absent (unchanged from the original inline
    lookups: plan is only fetched when a subscription row exists)."""
    tenant = workflows_domain_crud.get_tenant(db, tenant_id)
    subscription = None
    plan = None
    if tenant:
        subscription = workflows_domain_crud.get_subscription_for_tenant(db, tenant.id)
        if subscription:
            plan = workflows_domain_crud.get_plan(db, subscription.plan_id)
    return tenant, subscription, plan


def cancel_worker_tasks_for_run(db: Session, run_id: int) -> int:
    """Cancel every active ApiTaskQueue row for a run. Returns the count
    cancelled. Caller (the router) keeps the original try/except + logging
    around this call — this function owns only the query + mutation + commit,
    matching the original inline block exactly (commit only if any cancelled)."""
    active_tasks = workflows_domain_crud.get_active_tasks_for_run(db, run_id)
    cancelled = 0
    for task in active_tasks:
        task.status = models.ApiTaskStatus.CANCELLED
        cancelled += 1
    if cancelled > 0:
        db.commit()
    return cancelled


def block_node_runs_for_cancel(db: Session, run_id: int, *, completed_at: datetime, error_message: str) -> None:
    """Mark queued/running NodeRuns FAILED with the cancel message stamped, so
    the run-detail UI explains the FAIL came from a user cancel. Commits
    unconditionally (matches the original — even an empty list still commits)."""
    queued_nodes = workflows_domain_crud.get_active_node_runs_for_run(db, run_id)
    for nr in queued_nodes:
        nr.status = models.TaskStatus.FAILED
        nr.completed_at = completed_at
        nr.error_message = error_message
    db.commit()
