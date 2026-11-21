"""
Bulk Permission Management Endpoints

Allows administrators to perform batch operations on permissions:
- Grant access to multiple users or folders at once
- Revoke multiple permissions
- Extend expiration dates in bulk
- Update capability flags in bulk
"""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, status, HTTPException, BackgroundTasks, Request
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel

import models
import audit
import logging
from dependencies import get_db, get_current_user
from permissions import check_user_capability_on_cog, get_user_permission_on_cog, validate_permission_grant
from cog_grants import (  # Gate 30: sharing-grant read model
    list_cog_grants, get_cog_grant_by_id, revoke_cog_grant_by_id, extend_cog_grant,
    set_cog_grant_invitation_context, CogShareSpec)
from resource_permissions import sync_cog_permission_grants, revoke_stale_cog_share_caps
from models import PermissionLevel, PermissionTargetType
from services._base import ServiceError
import services.bulk_permissions_service as bulk_permissions_service

logger = logging.getLogger(__name__)

# Maps a ServiceError's `code` to the HTTP status the pre-migration handlers
# raised for that case (Gate 2b migration). `exc.message` carries the exact
# original detail text, so the response body is byte-identical to before.
_STATUS_BY_CODE = {
    "bad_request": status.HTTP_400_BAD_REQUEST,
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "conflict": status.HTTP_409_CONFLICT,
    "internal_error": status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _raise_http(exc: ServiceError):
    raise HTTPException(
        status_code=_STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.message,
    )


# --- Request/Response Schemas ---

class BulkGrantPermissionRequest(BaseModel):
    """Request to grant permissions in bulk."""
    # Target resources
    cog_ids: Optional[List[int]] = None  # Grant to multiple folders

    # Recipients
    user_ids: Optional[List[int]] = None  # Grant to multiple users
    emails: Optional[List[str]] = None  # Invite external users by email

    # Permission configuration
    permission_level: str  # viewer, editor, owner
    expires_in_days: Optional[int] = None

    # Capability overrides
    can_upload: Optional[bool] = None
    can_create_subfolders: Optional[bool] = None
    can_move_files: Optional[bool] = None
    can_process: Optional[bool] = None
    can_share: Optional[bool] = None
    can_delete: Optional[bool] = None

    # Invitation context
    invitation_message: Optional[str] = None


class BulkGrantResult(BaseModel):
    """Result of a single permission grant in bulk operation."""
    cog_id: int
    user_id: Optional[int] = None
    email: Optional[str] = None
    status: str  # "granted", "already_exists", "error"
    error_message: Optional[str] = None
    permission_id: Optional[int] = None


class BulkGrantResponse(BaseModel):
    """Response from bulk grant operation."""
    total_requested: int
    successful: int
    failed: int
    results: List[BulkGrantResult]


class BulkRevokeRequest(BaseModel):
    """Request to revoke multiple permissions."""
    permission_ids: List[int]


class BulkRevokeResult(BaseModel):
    """Result of a single permission revocation."""
    permission_id: int
    status: str  # "revoked", "not_found", "error"
    error_message: Optional[str] = None


class BulkRevokeResponse(BaseModel):
    """Response from bulk revoke operation."""
    total_requested: int
    successful: int
    failed: int
    results: List[BulkRevokeResult]


class BulkExtendExpirationRequest(BaseModel):
    """Request to extend expiration dates for multiple permissions."""
    permission_ids: List[int]
    extend_by_days: int  # Add this many days to current expiration


class BulkExtendResult(BaseModel):
    """Result of a single expiration extension."""
    permission_id: int
    old_expires_at: Optional[str] = None
    new_expires_at: Optional[str] = None
    status: str  # "extended", "no_expiration", "error"
    error_message: Optional[str] = None


class BulkExtendResponse(BaseModel):
    """Response from bulk extend operation."""
    total_requested: int
    successful: int
    failed: int
    results: List[BulkExtendResult]


class BulkUpdateCapabilitiesRequest(BaseModel):
    """Request to update capabilities for multiple permissions."""
    permission_ids: List[int]

    # Capabilities to update (None means don't change)
    can_upload: Optional[bool] = None
    can_create_subfolders: Optional[bool] = None
    can_move_files: Optional[bool] = None
    can_process: Optional[bool] = None
    can_share: Optional[bool] = None
    can_delete: Optional[bool] = None


class BulkUpdateResult(BaseModel):
    """Result of a single capability update."""
    permission_id: int
    status: str  # "updated", "not_found", "error"
    error_message: Optional[str] = None


class BulkUpdateResponse(BaseModel):
    """Response from bulk update operation."""
    total_requested: int
    successful: int
    failed: int
    results: List[BulkUpdateResult]


router = APIRouter(
    prefix="/bulk-permissions",
    tags=["Bulk Permissions"]
)


@router.post("/grant", response_model=BulkGrantResponse)
def bulk_grant_permissions(
        request: BulkGrantPermissionRequest,
        background_tasks: BackgroundTasks,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Grant permissions in bulk.

    Can be used to:
    - Grant one permission level to multiple users on one folder
    - Grant one user access to multiple folders
    - Grant multiple users access to multiple folders (cross product)

    User must have 'can_share' permission on all target folders.
    """
    try:
        result = bulk_permissions_service.bulk_grant_permissions(
            db, request, http_request, current_user
        )
        return BulkGrantResponse(**result)
    except ServiceError as exc:
        _raise_http(exc)


@router.post("/revoke", response_model=BulkRevokeResponse)
def bulk_revoke_permissions(
        request: BulkRevokeRequest,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Revoke multiple permissions at once.

    User must have 'can_share' permission on the folders or be the granter.
    """
    try:
        result = bulk_permissions_service.bulk_revoke_permissions(
            db, request, http_request, current_user
        )
        return BulkRevokeResponse(**result)
    except ServiceError as exc:
        _raise_http(exc)


@router.post("/extend-expiration", response_model=BulkExtendResponse)
def bulk_extend_expiration(
        request: BulkExtendExpirationRequest,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Extend expiration dates for multiple permissions at once.

    User must have 'can_share' permission on the folders or be the granter.
    """
    try:
        result = bulk_permissions_service.bulk_extend_expiration(
            db, request, current_user
        )
        return BulkExtendResponse(**result)
    except ServiceError as exc:
        _raise_http(exc)


@router.post("/update-capabilities", response_model=BulkUpdateResponse)
def bulk_update_capabilities(
        request: BulkUpdateCapabilitiesRequest,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Update capability flags for multiple permissions at once.

    User must have 'can_share' permission on the folders or be the granter.
    """
    try:
        result = bulk_permissions_service.bulk_update_capabilities(
            db, request, http_request, current_user
        )
        return BulkUpdateResponse(**result)
    except ServiceError as exc:
        _raise_http(exc)
