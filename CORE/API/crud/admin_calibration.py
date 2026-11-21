"""Admin calibration CRUD — data access for `HardwareProfile.calibration_status`.

`HardwareProfile` is a platform-global model (one row per deduplicated
CPU+GPU+RAM fingerprint shared across workers/tenants) — it has no
`tenant_id` and no `deleted_at` column, so these queries are intentionally
NOT tenant-scoped and NOT soft-delete filtered. That matches the pre-existing
router behavior 1:1 (see shared migration rule: port filters that exist,
don't invent scoping that isn't there). Access control (platform-admin only)
is enforced by the router's `_require_platform_admin` FastAPI dependency
before any of these functions run.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_by_id(db: Session, profile_id: int) -> Optional[models.HardwareProfile]:
    """Fetch one HardwareProfile by primary key. Returns None if absent."""
    return (
        db.query(models.HardwareProfile)
        .filter(models.HardwareProfile.id == profile_id)
        .first()
    )


def list_profiles(
    db: Session, *, status: Optional[str] = None
) -> List[models.HardwareProfile]:
    """List all hardware profiles, optionally filtered by calibration_status,
    ordered by id. Unbounded — matches the pre-existing (admin-only, small
    table) behavior; not a candidate for Page-style pagination in this port."""
    q = db.query(models.HardwareProfile)
    if status:
        q = q.filter(models.HardwareProfile.calibration_status == status)
    return q.order_by(models.HardwareProfile.id).all()


def set_status(
    db: Session, profile: models.HardwareProfile, status: str
) -> models.HardwareProfile:
    """Persist a new calibration_status on an already-fetched profile.

    Mirrors the pre-existing set_calibration_status behavior: transitioning to
    'calibrated' stamps last_calibrated_at when it isn't already set. Callers
    passing any other status (e.g. 'stale') leave last_calibrated_at untouched.
    """
    profile.calibration_status = status
    if status == "calibrated" and not profile.last_calibrated_at:
        profile.last_calibrated_at = datetime.now(timezone.utc)
    db.commit()
    return profile
