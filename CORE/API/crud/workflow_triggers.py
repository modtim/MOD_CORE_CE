"""CRUD layer for workflow triggers (Gate 2b service/CRUD migration).

The ONLY place `db.query(...)` lives for this domain (see
`services/_base.py` layering doc). Every function here is a pure read —
tenant-scoped where the model has a tenant, soft-delete filtered where the
model has `deleted_at`. Writes (create/update/soft-delete) stay in
`services/workflow_triggers_service.py`, matching the existing convention
in the domains migrated so far (e.g. `services/accelerators_service.py`
commits directly rather than routing through a CRUD writer).

Migrated out of `routers/workflow_triggers.py` — see that module for the
thin HTTP layer this feeds.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_tenant_workflow(
    db: Session, workflow_id: int, tenant_id: int
) -> Optional[models.Workflow]:
    """Fetch a workflow scoped to `tenant_id`. None if missing or owned by
    another tenant (the caller maps that to a 404 — cross-tenant existence
    is never distinguished from "doesn't exist")."""
    return db.query(models.Workflow).filter(
        models.Workflow.id == workflow_id,
        models.Workflow.tenant_id == tenant_id,
    ).first()


def get_trigger(
    db: Session, trigger_id: int, tenant_id: int
) -> Optional[models.WorkflowTrigger]:
    """Fetch a non-soft-deleted trigger scoped to `tenant_id`. None if
    missing, soft-deleted, or owned by another tenant."""
    return db.query(models.WorkflowTrigger).filter(
        models.WorkflowTrigger.id == trigger_id,
        models.WorkflowTrigger.tenant_id == tenant_id,
        models.WorkflowTrigger.deleted_at.is_(None),
    ).first()


def list_triggers(
    db: Session, tenant_id: int, *, workflow_id: Optional[int] = None
) -> List[models.WorkflowTrigger]:
    """List non-soft-deleted triggers for a tenant, optionally filtered by
    `workflow_id`, newest-id-first."""
    query = db.query(models.WorkflowTrigger).filter(
        models.WorkflowTrigger.tenant_id == tenant_id,
        models.WorkflowTrigger.deleted_at.is_(None),
    )
    if workflow_id:
        query = query.filter(models.WorkflowTrigger.workflow_id == workflow_id)
    return query.order_by(models.WorkflowTrigger.id.desc()).all()


def list_trigger_events(
    db: Session, trigger_id: int, tenant_id: int, *, limit: int
) -> List[models.TriggerEventLog]:
    """Recent event-arrival rows for one trigger, tenant-scoped, bounded by
    `limit` (caller clamps the window before calling)."""
    return (
        db.query(models.TriggerEventLog)
        .filter(
            models.TriggerEventLog.trigger_id == trigger_id,
            models.TriggerEventLog.tenant_id == tenant_id,
        )
        .order_by(models.TriggerEventLog.fired_at.desc())
        .limit(limit)
        .all()
    )
