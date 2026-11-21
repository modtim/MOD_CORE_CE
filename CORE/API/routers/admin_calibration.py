"""Admin calibration endpoints — manual control over per-profile staleness.

Platform admin only. Lets ops force a HardwareProfile into calibration_status='stale'
when they know something has changed underneath the system (driver swap, kernel
upgrade, hardware reseat, BIOS update) that the heartbeat-driven detection
hasn't caught yet, OR to clear a stale profile back to calibrated when they've
confirmed nothing has actually changed (rare; usually you'd just let the
auto-recalibration run).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services import admin_calibration_service
from services._base import ServiceError

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Admin / Calibration"],
    prefix="/platform/hardware-profiles",
)

# Re-exported for any external importer of the pre-migration module-level
# constant; canonical copy now lives in services/admin_calibration_service.py.
VALID_STATUSES = admin_calibration_service.VALID_STATUSES


def _require_platform_admin(current_user: models.User = Depends(require_active_and_onboarded_user)):
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")
    return current_user


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer `ServiceError` to the `HTTPException` this router
    always raised directly before the Gate 2b migration (same status/detail
    shape — a plain string, not the (not-yet-installed) error envelope)."""
    status = {
        "forbidden": 403,
        "not_found": 404,
        "unprocessable_entity": 422,
        "bad_request": 400,
    }.get(exc.code, 400)
    raise HTTPException(status_code=status, detail=exc.message)


class HardwareProfileStatusResponse(BaseModel):
    id: int
    fingerprint: str
    calibration_status: str
    last_calibrated_at: Optional[datetime] = None
    cpu_model: Optional[str] = None
    gpu_model: Optional[str] = None


class SetCalibrationStatusRequest(BaseModel):
    status: str = Field(..., description="One of: uncalibrated, calibrating, calibrated, stale")
    reason: Optional[str] = Field(default=None, description="Free-form reason for the change (audit trail)")


@router.post(
    "/{profile_id}/mark-stale",
    response_model=HardwareProfileStatusResponse,
    summary="Force a HardwareProfile to 'stale' (admin manual override)",
)
def mark_hardware_profile_stale(
    profile_id: int,
    reason: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_platform_admin),
):
    """Flip the profile's calibration_status to 'stale'.

    The orchestrator picks up stale profiles on the next staleness eval
    sweep and queues recalibration tasks for them (capacity-floor aware).
    This is the manual-override version — same destination as the
    automated driver-change / package-delta / time-based triggers.
    """
    try:
        profile = admin_calibration_service.mark_stale(
            db,
            profile_id=profile_id,
            reason=reason,
            admin_user_id=current_user.id,
        )
        return HardwareProfileStatusResponse(
            id=profile.id,
            fingerprint=profile.fingerprint,
            calibration_status=profile.calibration_status,
            last_calibrated_at=profile.last_calibrated_at,
            cpu_model=profile.cpu_model,
            gpu_model=profile.gpu_model,
        )
    except ServiceError as exc:
        _raise_http(exc)


@router.post(
    "/{profile_id}/calibration-status",
    response_model=HardwareProfileStatusResponse,
    summary="Set calibration_status to a specific value (admin escape hatch)",
)
def set_calibration_status(
    profile_id: int,
    body: SetCalibrationStatusRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_platform_admin),
):
    """Direct setter for calibration_status. Used when admin needs to
    move a profile to a non-stale status (e.g. revert a false-positive
    stale flag, mark as uncalibrated to force a full re-bootstrap)."""
    try:
        profile = admin_calibration_service.set_status(
            db,
            profile_id=profile_id,
            status=body.status,
            reason=body.reason,
            admin_user_id=current_user.id,
        )
        return HardwareProfileStatusResponse(
            id=profile.id,
            fingerprint=profile.fingerprint,
            calibration_status=profile.calibration_status,
            last_calibrated_at=profile.last_calibrated_at,
            cpu_model=profile.cpu_model,
            gpu_model=profile.gpu_model,
        )
    except ServiceError as exc:
        _raise_http(exc)


@router.get(
    "",
    response_model=list[HardwareProfileStatusResponse],
    summary="List all HardwareProfiles with calibration status (admin)",
)
def list_hardware_profiles(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_platform_admin),
):
    """List all hardware profiles, optionally filtered by calibration_status.

    Useful for admin UI: show how many profiles are stale and need recalibration.
    """
    try:
        profiles = admin_calibration_service.list_profiles(db, status=status)
    except ServiceError as exc:
        _raise_http(exc)
        return  # unreachable; _raise_http always raises

    return [
        HardwareProfileStatusResponse(
            id=p.id,
            fingerprint=p.fingerprint,
            calibration_status=p.calibration_status,
            last_calibrated_at=p.last_calibrated_at,
            cpu_model=p.cpu_model,
            gpu_model=p.gpu_model,
        )
        for p in profiles
    ]
