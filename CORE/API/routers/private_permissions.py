"""
Private Permission Management Endpoints

These endpoints are called by internal services (APScheduler jobs / service-account callers) for:
- Checking expiring permissions
- Sending expiration warning emails
- Automatically revoking expired permissions
"""

from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, status, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel

import models
from dependencies import get_db, require_service_api_key
from services import private_permissions_service as service
from services._base import ServiceError


# --- Request/Response Schemas ---

class ExpiringPermissionsResponse(BaseModel):
    """Response containing permissions expiring in different time periods."""
    expiring_7d: List[Dict[str, Any]]
    expiring_3d: List[Dict[str, Any]]
    expiring_1d: List[Dict[str, Any]]
    expired: List[Dict[str, Any]]


class ExpirationWarningSendRequest(BaseModel):
    """Request to send an expiration warning."""
    days_until_expiration: int
    warning_type: str  # "7d", "3d", "1d"


class PermissionCheckRequest(BaseModel):
    """Request to check if a user has a specific capability."""
    user_id: int
    cog_id: int
    capability: str  # "can_upload", "can_process", etc.


class PermissionCheckResponse(BaseModel):
    """Response for permission check."""
    user_id: int
    cog_id: int
    capability: str
    has_permission: bool
    permission_level: Optional[str] = None
    is_owner: bool = False


class ExpirationWarningSendResponse(BaseModel):
    """Response for a queued expiration warning (api_lint R3, task 1461)."""
    status: str
    permission_id: int
    warning_type: str
    recipient_email: str


class TrackNotificationResponse(BaseModel):
    """Response for a notification-channel tracking write (api_lint R3, task 1461).

    `warning_type` and `channels` are read from the caller-supplied dict with
    `.get()`, so they can be absent/null — kept Optional with no invented default.
    """
    status: str
    permission_id: int
    warning_type: Optional[str] = None
    channels: Optional[Dict[str, Any]] = None


router = APIRouter(
    prefix="/private/permissions",
    tags=["Private - Permissions"],
    dependencies=[Depends(require_service_api_key)]
)


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer error to the exact HTTPException shape this router
    always raised (plain string `detail`, no envelope) — kept inline rather
    than adopting api_conventions.error_response so the contract snapshot
    (status codes / detail shape) doesn't move."""
    status_code = {"not_found": status.HTTP_404_NOT_FOUND, "not_expired": status.HTTP_400_BAD_REQUEST}.get(
        exc.code, status.HTTP_400_BAD_REQUEST
    )
    raise HTTPException(status_code=status_code, detail=exc.message)


@router.get("/expiring", response_model=ExpiringPermissionsResponse)
def get_expiring_permissions(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_service_api_key)
):
    """
    Get all permissions expiring soon or already expired.

    Uses tenant-specific warning schedules from TenantPermissionSettings.
    Falls back to default [7, 3, 1] schedule if tenant has no custom config.

    Returns buckets for:
    - 7 days from now (±6 hours) - or custom schedule
    - 3 days from now (±6 hours) - or custom schedule
    - 1 day from now (±6 hours) - or custom schedule
    - Already expired
    """
    buckets = service.list_expiring_permissions(db)
    return ExpiringPermissionsResponse(**buckets)


@router.post(
    "/{permission_id}/send-expiration-warning",
    response_model=ExpirationWarningSendResponse,
    status_code=status.HTTP_200_OK,
)
def send_expiration_warning(
        permission_id: int,
        request: ExpirationWarningSendRequest,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_service_api_key)
):
    """
    Send an expiration warning email for a specific permission.

    Sends to:
    - The user who has the permission
    - The user who granted the permission (if different)
    - Tenant admins (optional)
    """
    try:
        return service.send_expiration_warning(
            db,
            permission_id,
            days_until_expiration=request.days_until_expiration,
            warning_type=request.warning_type,
        )
    except ServiceError as exc:
        _raise_http(exc)


@router.post(
    "/{permission_id}/revoke-expired",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_expired_permission(
        permission_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_service_api_key)
):
    """
    Revoke a permission that has already expired.

    This is called automatically by the daily expiration check DAG.
    Uses soft-delete for ISO 27001 compliance (audit trail preservation).
    """
    try:
        service.revoke_expired_permission(db, permission_id)
    except ServiceError as exc:
        _raise_http(exc)

    return


@router.post("/check-capability", response_model=PermissionCheckResponse)
def check_user_capability(
        request: PermissionCheckRequest,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_service_api_key)
):
    """
    Check if a user has a specific capability on a folder.

    This endpoint is used by worker containers and other service callers
    to verify permissions before executing operations on folders.

    Returns permission details including whether user is owner and
    their current permission level.
    """
    result = service.check_user_capability(
        db, user_id=request.user_id, cog_id=request.cog_id, capability=request.capability
    )
    return PermissionCheckResponse(**result)


@router.post(
    "/{permission_id}/track-notification",
    response_model=TrackNotificationResponse,
    status_code=status.HTTP_200_OK,
)
def track_notification_sent(
        permission_id: int,
        tracking_data: Dict[str, Any],
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_service_api_key)
):
    """
    Track which notification channels successfully sent for a permission warning.

    This prevents duplicate notifications and provides audit trail.

    Args:
        tracking_data: Dict with warning_type and channels status
    """
    try:
        return service.track_notification(db, permission_id, tracking_data)
    except ServiceError as exc:
        _raise_http(exc)
