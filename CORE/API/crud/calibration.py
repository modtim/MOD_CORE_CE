"""Calibration CRUD — data access for `CalibrationPlan` (the coordinator-driven
batch-calibration plan, one row per tenant "run").

Query layer only (openspec: stack-training-and-prewarm, D9): tenant scoping
lives here, once, instead of inline in routers/calibration.py. `CalibrationPlan`
has no `deleted_at` column (not in database.py's soft-delete listener — see
MEMORY iso-no-hard-delete / the shared migration rule: port filters 1:1, don't
invent soft-delete scoping that isn't there), so these functions intentionally
do not filter on it.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from models import CalibrationPlan


def get_latest_plan_for_tenant(db: Session, tenant_id: int) -> Optional[CalibrationPlan]:
    """Most recent plan row for a tenant (any status), or None if the tenant
    has never started one. Mirrors the pre-existing inline query 1:1."""
    return (
        db.query(CalibrationPlan)
        .filter(CalibrationPlan.tenant_id == tenant_id)
        .order_by(CalibrationPlan.id.desc())
        .first()
    )


def get_running_plan_for_tenant(db: Session, tenant_id: int) -> Optional[CalibrationPlan]:
    """The tenant's currently-running plan, if any."""
    return (
        db.query(CalibrationPlan)
        .filter(
            CalibrationPlan.tenant_id == tenant_id,
            CalibrationPlan.status == "running",
        )
        .first()
    )
