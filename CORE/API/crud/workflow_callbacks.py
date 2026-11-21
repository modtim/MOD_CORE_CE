"""CRUD (query layer) for workflow completion callbacks.

Gate 2b migration of `routers/workflow_callbacks.py`. Only the query layer
lives here: a tenant-UNSCOPED fetch (tenant ownership is a service-layer
policy check — see migrate_rules #5, the 404-vs-403 trap: scoping the fetch
itself would turn a legitimate 403 into a masking 404) and the conditional
terminal-status transition.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

import models


def get_workflow_run_by_id(db: Session, workflow_run_id: int) -> Optional[models.WorkflowRun]:
    """Fetch a workflow run by id, tenant-UNSCOPED. Tenant ownership is
    checked by the service layer so a mismatch can raise a real 403 instead
    of a query-scoped 404."""
    return (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.id == workflow_run_id)
        .first()
    )


def transition_to_terminal(
    db: Session, workflow_run_id: int, new_status: "models.TaskStatus"
) -> int:
    """Conditionally move a run to a terminal status iff it is still
    QUEUED/RUNNING (prevents double-processing a callback delivered twice).

    Returns the number of rows updated (0 or 1). Commits only when a row was
    actually updated, matching the original inline handler's behaviour.
    """
    result = db.execute(
        update(models.WorkflowRun)
        .where(
            models.WorkflowRun.id == workflow_run_id,
            models.WorkflowRun.status.in_(
                [models.TaskStatus.QUEUED, models.TaskStatus.RUNNING]
            ),
        )
        .values(status=new_status, completed_at=datetime.now(timezone.utc))
    )
    if result.rowcount > 0:
        db.commit()
    return result.rowcount
