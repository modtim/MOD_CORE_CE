"""node_definitions `workflows` sub-module CRUD — the ONLY place
`db.query(models.X)` runs for `routers/node_definitions_domains/workflows.py`
(Gate 2b migration, openspec: stack-training-and-prewarm).

Four query shapes, matching the pre-migration inline queries plus the
standard tenant + soft-delete filtering this migration adds:

  * `get_workflow_run_for_tenant(...)` — tenant-scoped fetch of the build run
    under a rebuild request (/wizard/rebuild-variant). 404-vs-403 trap (shared
    migration rule #5): this DOES filter on tenant_id (a run belonging to
    another tenant 404s here, exactly as the pre-migration inline query,
    which combined the id + tenant_id filter in one `.filter(...)` call).
    The *caller* ownership check (`triggered_by_user_id`) happens after, on
    the fetched row, in the service — a tenant-scoped fetch does not
    collapse that legitimate 403, because ownership is a different caller
    than tenant membership. `deleted_at IS NULL` is added here (the
    pre-migration query had no soft-delete guard); `WorkflowRun` carries
    `SoftDeleteWithActorMixin` so a soft-deleted run should not be
    rebuildable.

  * `get_latest_build_spec_for_run(...)` — tenant-scoped, most-recent
    (non-deleted) BuildSpecRecord for a given workflow_run_id.

  * `get_workflow_by_id(...)` — tenant-UNSCOPED lookup of the just-created
    rebuild Workflow row. Matches the original inline query, which never
    filtered on tenant_id here (the workflow id came from
    `create_build_workflow`, called moments earlier in the same tenant
    context — re-scoping here would be redundant, not a security boundary).
    `deleted_at IS NULL` added for consistency; harmless since the row is
    freshly created by the same request.

  * `get_tenant_by_id(...)` — fetch the caller's own Tenant row
    (`quota_push_blocked` check in /build-and-register-local). Filters on id
    only, matching the original; the id passed is always
    `current_user.tenant_id`, i.e. never attacker-controlled.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_workflow_run_for_tenant(
    db: Session, run_id: int, tenant_id: Optional[int],
) -> Optional["models.WorkflowRun"]:
    return db.query(models.WorkflowRun).filter(
        models.WorkflowRun.id == run_id,
        models.WorkflowRun.tenant_id == tenant_id,
        models.WorkflowRun.deleted_at.is_(None),
    ).first()


def get_latest_build_spec_for_run(
    db: Session, workflow_run_id: int, tenant_id: Optional[int],
) -> Optional["models.BuildSpecRecord"]:
    return db.query(models.BuildSpecRecord).filter(
        models.BuildSpecRecord.workflow_run_id == workflow_run_id,
        models.BuildSpecRecord.tenant_id == tenant_id,
        models.BuildSpecRecord.deleted_at.is_(None),
    ).order_by(models.BuildSpecRecord.id.desc()).first()


def get_workflow_by_id(db: Session, workflow_id) -> Optional["models.Workflow"]:
    return db.query(models.Workflow).filter(
        models.Workflow.id == workflow_id,
        models.Workflow.deleted_at.is_(None),
    ).first()


def get_tenant_by_id(db: Session, tenant_id) -> Optional["models.Tenant"]:
    return db.query(models.Tenant).filter(
        models.Tenant.id == tenant_id,
        models.Tenant.deleted_at.is_(None),
    ).first()
