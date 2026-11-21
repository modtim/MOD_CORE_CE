"""node_definitions `node_wizard` sub-module CRUD — the ONLY place
`db.query(models.X)` runs for `routers/node_definitions_domains/node_wizard.py`
(Gate 2b migration, openspec: stack-training-and-prewarm).

Four query shapes, each matching the pre-migration inline query exactly:

  * `list_tenant_node_definitions(...)` — every `NodeDefinition` row owned by
    the caller's tenant, used by `build_and_register_node`'s name/slug
    collision guard. **Deliberately NOT filtered on `deleted_at`** — the
    pre-migration inline query never applied a soft-delete filter here
    either, so a soft-deleted node's name/slug still blocks a fresh
    registration. This is preserved as-is (pure behaviour-preserving MOVE);
    it may be a pre-existing gap (ISO soft-delete rows shouldn't necessarily
    keep squatting a name forever) but changing it is out of scope for this
    migration — flagged, not fixed.

  * `get_workflow_by_id(...)` — tenant-**UNSCOPED** lookup of the `Workflow`
    row `create_build_workflow` just created, used only to hand it to
    `dispatch_trigger_run`. Matches the original inline query (no tenant
    filter); safe because the id was produced moments earlier by the same
    request for the caller's own tenant.

  * `get_workflow_run_for_tenant(...)` — tenant-scoped `WorkflowRun` fetch
    backing `/build-run-progress/{workflow_run_id}`'s first 404 gate. The
    service layer applies a SECOND check (`can_view_workflow_run`) on top of
    this and both failure modes return 404 (there is no 403 branch on this
    endpoint — see the service module docstring).

  * `list_child_tasks_for_run(...)` — every `ApiTaskQueue` row belonging to a
    workflow_run, oldest-first, used to build the aggregated `log_tail`.
    Not tenant-filtered directly (matches the original inline query); the
    caller already passed the tenant + permission gate on the parent
    `WorkflowRun` above before this is called.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def list_tenant_node_definitions(db: Session, tenant_id: int) -> List["models.NodeDefinition"]:
    """Every `NodeDefinition` row for `tenant_id` (no soft-delete filter —
    see module docstring)."""
    return db.query(models.NodeDefinition).filter(
        models.NodeDefinition.tenant_id == tenant_id,
    ).all()


def get_workflow_by_id(db: Session, workflow_id: int) -> Optional["models.Workflow"]:
    """Fetch a `Workflow` row by id, tenant-UNSCOPED (see module docstring)."""
    return db.query(models.Workflow).filter(
        models.Workflow.id == workflow_id,
    ).first()


def get_workflow_run_for_tenant(
    db: Session, workflow_run_id: int, tenant_id: int,
) -> Optional["models.WorkflowRun"]:
    """Fetch a `WorkflowRun` by id, scoped to `tenant_id`."""
    return db.query(models.WorkflowRun).filter(
        models.WorkflowRun.id == workflow_run_id,
        models.WorkflowRun.tenant_id == tenant_id,
    ).first()


def list_child_tasks_for_run(db: Session, workflow_run_id: int) -> List["models.ApiTaskQueue"]:
    """Every `ApiTaskQueue` row for `workflow_run_id`, oldest-first."""
    return (
        db.query(models.ApiTaskQueue)
        .filter(models.ApiTaskQueue.workflow_run_id == workflow_run_id)
        .order_by(models.ApiTaskQueue.id.asc())
        .all()
    )
