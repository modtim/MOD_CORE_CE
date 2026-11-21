"""CRUD layer for `routers/trigger_fire.py` (Gate 2b service/CRUD migration).

The ONLY place `db.query(...)` lives for the inbound-trigger-fire domain.
Every function here is a pure read for a `WorkflowTrigger`, always excluding
soft-deleted rows (`deleted_at IS NULL`) and always requiring `is_active`
(a trigger that exists but is inactive must be indistinguishable from one
that doesn't exist, at the response level — see the service module).

None of these apply a `tenant_id` filter in SQL: trigger_fire's authenticated
endpoints (api/manual/approval) look a trigger up by id alone and then compare
`trig.tenant_id != user.tenant_id` in Python, collapsing a foreign-tenant
trigger into the exact same 404 as a missing one (no enumeration oracle on
sequential trigger ids). That comparison is business logic and stays in
`services/trigger_fire_service.py`, unchanged from how it lived in the router
before this migration.

Migrated out of `routers/trigger_fire.py` — see that module for the thin HTTP
layer this feeds, and `services/trigger_fire_service.py` for the business
logic (secret verification, signature verification, dispatch orchestration).
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_webhook_trigger(db: Session, trigger_id: int) -> Optional[models.WorkflowTrigger]:
    """Fetch a non-soft-deleted, active WEBHOOK trigger by id.

    None if missing, wrong type, soft-deleted, or inactive — the caller
    (service) folds all of those into one identical 404 response, matching
    the pre-migration router's `if not trig or not trig.is_active` check.
    """
    return (
        db.query(models.WorkflowTrigger)
        .filter(
            models.WorkflowTrigger.id == trigger_id,
            models.WorkflowTrigger.trigger_type == models.WorkflowTriggerType.WEBHOOK,
            models.WorkflowTrigger.deleted_at.is_(None),
            models.WorkflowTrigger.is_active.is_(True),
        )
        .first()
    )


def get_active_trigger(
    db: Session, trigger_id: int, trigger_type: models.WorkflowTriggerType
) -> Optional[models.WorkflowTrigger]:
    """Fetch a non-soft-deleted, active trigger of `trigger_type` by id.

    None if missing, wrong type, soft-deleted, or inactive. Equivalent to the
    pre-migration router's `_active()` helper (which fetched without the
    `is_active` filter and then checked `t.is_active` in Python) — folding the
    check into the query is behaviourally identical since both paths return
    "not found" for an inactive row.
    """
    return (
        db.query(models.WorkflowTrigger)
        .filter(
            models.WorkflowTrigger.id == trigger_id,
            models.WorkflowTrigger.trigger_type == trigger_type,
            models.WorkflowTrigger.deleted_at.is_(None),
            models.WorkflowTrigger.is_active.is_(True),
        )
        .first()
    )


def list_active_triggers_of_type(
    db: Session, trigger_type: models.WorkflowTriggerType
) -> List[models.WorkflowTrigger]:
    """All non-soft-deleted, active triggers of `trigger_type`, any tenant.

    Email/chat-ops fan-out matches candidates by channel configuration (a
    `to` address, a Slack/Teams signature), not by tenant — exactly as the
    pre-migration router's `_active_of_type()` helper did.
    """
    return (
        db.query(models.WorkflowTrigger)
        .filter(
            models.WorkflowTrigger.trigger_type == trigger_type,
            models.WorkflowTrigger.is_active.is_(True),
            models.WorkflowTrigger.deleted_at.is_(None),
        )
        .all()
    )
