# API/crud/interventions.py
"""Data access for the manual-intervention (Gate 5 human-approval) domain
(routers/interventions.py).

The ONLY two `db.query(models.X)` call sites this domain has: the
active-user list an approval can be assigned to, and the downstream
NodeRun scan run on rejection to mark stranded nodes BLOCKED.
`crud.get_user_assigned_tasks` (crud/user_cud.py) already served
`GET /interventions` before this migration and is shared with
routers/me/iam_permissions.py, so it stays where it is -- not duplicated
here.

`ManualInterventionState` has no `tenant_id` or `deleted_at` column (it
is not in database.py's soft-delete listener) -- tenant scoping for a
decision is enforced indirectly via `assigned_to_user_id`, ported 1:1 in
services/interventions_service.py. Do not "fix" that by adding a
tenant_id filter here.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def list_active_tenant_users(db: Session, tenant_id: int) -> List[models.User]:
    """ACTIVE users in a tenant, ordered by id -- candidates a
    `human_approval` node may name as its approver."""
    return (
        db.query(models.User)
        .filter(
            models.User.tenant_id == tenant_id,
            models.User.status == models.UserStatus.ACTIVE,
        )
        .order_by(models.User.id)
        .all()
    )


def list_stranded_node_runs(
    db: Session,
    workflow_run_id: int,
    *,
    exclude_node_run_id: Optional[int] = None,
) -> List[models.NodeRun]:
    """NodeRuns on a run still QUEUED/RUNNING/PENDING_ACTION -- the set a
    rejection marks BLOCKED. `exclude_node_run_id` leaves out the gate's
    own NodeRun, whose FAILED assignment may not have flushed yet, so it
    would otherwise match its own row and get overwritten."""
    query = db.query(models.NodeRun).filter(
        models.NodeRun.workflow_run_id == workflow_run_id,
        models.NodeRun.status.in_([
            models.TaskStatus.QUEUED,
            models.TaskStatus.RUNNING,
            models.TaskStatus.PENDING_ACTION,
        ]),
    )
    if exclude_node_run_id is not None:
        query = query.filter(models.NodeRun.id != exclude_node_run_id)
    return query.all()
