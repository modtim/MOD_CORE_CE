"""Admin calibration service — policy for `HardwareProfile.calibration_status`.

Platform-admin only. The FastAPI dependency `_require_platform_admin` in
`routers/admin_calibration.py` resolves BEFORE any handler body runs (per the
shared migration rule, auth `Depends()` guards stay in the router unchanged —
a `ServiceError` raised there would escape the router's try/except as a 500).
This service assumes the caller has already been authorized and owns:

    * calibration_status transition validation (VALID_STATUSES)
    * the last_calibrated_at stamping policy (delegated to the CRUD layer,
      which is the one place the write happens)
    * the admin audit-trail log line for every status change

No HTTP concerns here — raises `services._base.ServiceError` subclasses; the
router maps them to `HTTPException`.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy.orm import Session

import models
from crud import admin_calibration as calibration_crud
from services._base import NotFoundError, ServiceError

logger = logging.getLogger(__name__)

VALID_STATUSES = {"uncalibrated", "calibrating", "calibrated", "stale"}


class InvalidStatusError(ServiceError):
    """Requested calibration_status isn't one of VALID_STATUSES (router -> 422)."""

    def __init__(self, status: str):
        super().__init__(
            f"status must be one of {sorted(VALID_STATUSES)}",
            code="unprocessable_entity",
            details={"status": status, "allowed": sorted(VALID_STATUSES)},
        )


def mark_stale(
    db: Session,
    *,
    profile_id: int,
    reason: Optional[str],
    admin_user_id: int,
) -> models.HardwareProfile:
    """Force a HardwareProfile to 'stale' (manual admin override).

    The orchestrator picks up stale profiles on the next staleness eval sweep
    and queues recalibration tasks for them (capacity-floor aware). Same
    destination as the automated driver-change / package-delta / time-based
    triggers — this is the manual-override version.
    """
    profile = calibration_crud.get_by_id(db, profile_id)
    if profile is None:
        raise NotFoundError(f"HardwareProfile {profile_id} not found")

    prior = profile.calibration_status
    profile = calibration_crud.set_status(db, profile, "stale")

    logger.info(
        "admin %s flipped hardware_profile=%s to stale (was=%s, reason=%s)",
        admin_user_id, profile_id, prior, reason or "unspecified",
    )
    return profile


def set_status(
    db: Session,
    *,
    profile_id: int,
    status: str,
    reason: Optional[str],
    admin_user_id: int,
) -> models.HardwareProfile:
    """Direct setter for calibration_status (admin escape hatch). Used when an
    admin needs to move a profile to a non-stale status (e.g. revert a
    false-positive stale flag, mark as uncalibrated to force a full
    re-bootstrap)."""
    if status not in VALID_STATUSES:
        raise InvalidStatusError(status)

    profile = calibration_crud.get_by_id(db, profile_id)
    if profile is None:
        raise NotFoundError(f"HardwareProfile {profile_id} not found")

    prior = profile.calibration_status
    profile = calibration_crud.set_status(db, profile, status)

    logger.info(
        "admin %s set hardware_profile=%s calibration_status=%s (was=%s, reason=%s)",
        admin_user_id, profile_id, status, prior, reason or "unspecified",
    )
    return profile


def list_profiles(
    db: Session, *, status: Optional[str] = None
) -> List[models.HardwareProfile]:
    """List all hardware profiles, optionally filtered by calibration_status.

    Useful for the admin UI: show how many profiles are stale and need
    recalibration.
    """
    if status and status not in VALID_STATUSES:
        raise InvalidStatusError(status)
    return calibration_crud.list_profiles(db, status=status)
