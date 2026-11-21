"""
Permission Request Endpoints

Allows users to request access to folders and admins to approve/deny requests.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, status, BackgroundTasks, Request
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel, ConfigDict
import os

import models
import audit
from dependencies import get_db, get_current_user

import services.permission_requests_service as permission_requests_service
from services._base import ServiceError


# --- Request/Response Schemas ---

class PermissionRequestCreate(BaseModel):
    """Request to create a permission request."""
    cog_id: int
    requested_level: str  # viewer, editor, owner
    requested_duration_days: Optional[int] = None
    justification: Optional[str] = None


class PermissionRequestResponse(BaseModel):
    """Response model for a permission request (cog or workflow)."""
    id: int
    tenant_id: int
    requester_id: int
    requester_name: str
    requester_email: str
    # Generic resource identity. cog_id/cog_name kept for back-compat with the
    # existing cog UI; resource_type/resource_id/resource_name are the generic
    # fields that also cover workflows.
    resource_type: str = "cog"
    resource_id: Optional[int] = None
    resource_name: Optional[str] = None
    cog_id: Optional[int] = None
    cog_name: Optional[str] = None
    requested_level: str
    requested_duration_days: Optional[int]
    justification: Optional[str]
    status: str
    reviewed_by_user_id: Optional[int]
    reviewer_name: Optional[str]
    reviewed_at: Optional[datetime]
    review_notes: Optional[str]
    granted_permission_id: Optional[int]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


def _resource_name_for(db: Session, req: "models.PermissionRequest") -> str:
    """Resolve the display name of a request's target (cog or workflow)."""
    return permission_requests_service.resource_display_name(db, req)


def _request_to_response(db: Session, req: "models.PermissionRequest") -> PermissionRequestResponse:
    """Build a response for either a cog or a workflow request."""
    requester, reviewer = permission_requests_service.requester_and_reviewer(db, req)
    name = permission_requests_service.resource_display_name(db, req)
    rtype = req.resource_type or "cog"
    rid = req.resource_id if req.resource_id is not None else req.cog_id
    return PermissionRequestResponse(
        id=req.id,
        tenant_id=req.tenant_id,
        requester_id=req.requester_id,
        requester_name=f"{requester.first_name} {requester.last_name}" if requester else "Unknown",
        requester_email=requester.email if requester else "Unknown",
        resource_type=rtype,
        resource_id=rid,
        resource_name=name,
        cog_id=req.cog_id,
        cog_name=name if rtype == "cog" else None,
        requested_level=req.requested_level.value,
        requested_duration_days=req.requested_duration_days,
        justification=req.justification,
        status=req.status.value,
        reviewed_by_user_id=req.reviewed_by_user_id,
        reviewer_name=f"{reviewer.first_name} {reviewer.last_name}" if reviewer else None,
        reviewed_at=req.reviewed_at,
        review_notes=req.review_notes,
        granted_permission_id=req.granted_permission_id,
        created_at=req.created_at,
        updated_at=req.updated_at,
    )


class ReviewRequestRequest(BaseModel):
    """Request to approve or deny a permission request."""
    action: str  # "approve" or "deny"
    review_notes: Optional[str] = None
    # For approvals: optional overrides
    override_level: Optional[str] = None  # Override requested level
    override_duration_days: Optional[int] = None  # Override requested duration


router = APIRouter(
    prefix="/permission-requests",
    tags=["Permission Requests"]
)


@router.post("", response_model=PermissionRequestResponse, status_code=status.HTTP_201_CREATED)
def create_permission_request(
        request: PermissionRequestCreate,
        background_tasks: BackgroundTasks,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Create a new permission request.

    Users can request access to folders they don't currently have permissions for.
    """
    try:
        permission_request, cog = permission_requests_service.create_request(
            db,
            tenant_id=current_user.tenant_id,
            requester=current_user,
            cog_id=request.cog_id,
            requested_level=request.requested_level,
            requested_duration_days=request.requested_duration_days,
            justification=request.justification,
        )
    except ServiceError as exc:
        permission_requests_service.raise_http(exc)

    # Log the permission request creation
    audit.log_permission_request_created(
        db=db,
        actor_user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        resource_type="cog",
        resource_id=request.cog_id,
        permission_request_id=permission_request.id,
        permission_level=permission_request.requested_level.value,
        metadata={
            "justification": request.justification,
            "requested_duration_days": request.requested_duration_days,
        },
        request=http_request,
    )

    # Send notification to folder owner
    owner = permission_requests_service.get_user(db, cog.owner_id)
    if owner:
        duration_text = f"{request.requested_duration_days} days" if request.requested_duration_days else "Permanent"

        notification_conf = {
            "notification_type": "new_request",
            "recipient_email": owner.email,
            "recipient_name": f"{owner.first_name} {owner.last_name}",
            "requester_name": f"{current_user.first_name} {current_user.last_name}",
            "requester_email": current_user.email,
            "folder_name": cog.name,
            "requested_level": permission_request.requested_level.value,
            "duration": duration_text,
            "requested_date": permission_request.created_at.strftime("%Y-%m-%d"),
            "justification": request.justification,
            "dashboard_url": f"{os.getenv('DASHBOARD_URL', 'http://dashboard.localhost')}/#permission-requests",
            "tenant_id": current_user.tenant_id
        }

        from utils.email_sender import send_permission_request_email
        background_tasks.add_task(
            send_permission_request_email,
            db=db,
            tenant_id=current_user.tenant_id,
            recipient_email=owner.email,
            recipient_name=f"{owner.first_name} {owner.last_name}",
            request_details=notification_conf,
        )

    # Prepare response
    return PermissionRequestResponse(
        id=permission_request.id,
        tenant_id=permission_request.tenant_id,
        requester_id=permission_request.requester_id,
        requester_name=f"{current_user.first_name} {current_user.last_name}",
        requester_email=current_user.email,
        cog_id=permission_request.cog_id,
        cog_name=cog.name,
        requested_level=permission_request.requested_level.value,
        requested_duration_days=permission_request.requested_duration_days,
        justification=permission_request.justification,
        status=permission_request.status.value,
        reviewed_by_user_id=None,
        reviewer_name=None,
        reviewed_at=None,
        review_notes=None,
        granted_permission_id=None,
        created_at=permission_request.created_at,
        updated_at=permission_request.updated_at
    )


@router.get("/my-requests", response_model=List[PermissionRequestResponse])
def get_my_permission_requests(
        status_filter: Optional[str] = None,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Get all permission requests created by the current user.

    Optionally filter by status: pending, approved, denied, cancelled
    """
    try:
        requests = permission_requests_service.list_my_requests(
            db, requester_id=current_user.id, status_filter=status_filter
        )
    except ServiceError as exc:
        permission_requests_service.raise_http(exc)

    # Generic builder handles both cog and workflow requests.
    return [_request_to_response(db, req) for req in requests]


@router.get("/pending", response_model=List[PermissionRequestResponse])
def get_pending_permission_requests(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Get all pending permission requests for folders the current user owns or has admin access to.

    This shows requests that need the current user's review.
    """
    requests = permission_requests_service.list_pending_for_reviewer(db, current_user=current_user)
    return [_request_to_response(db, req) for req in requests]


@router.post("/{request_id}/review", response_model=PermissionRequestResponse)
def review_permission_request(
        request_id: int,
        review: ReviewRequestRequest,
        background_tasks: BackgroundTasks,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Approve or deny a permission request.

    Only the folder owner or admins can review requests.
    """
    try:
        permission_request = permission_requests_service.get_reviewable_request(
            db, request_id=request_id, tenant_id=current_user.tenant_id
        )
        permission_request, permission, perm_level = permission_requests_service.apply_review(
            db,
            req=permission_request,
            reviewer=current_user,
            action=review.action,
            review_notes=review.review_notes,
            override_level=review.override_level,
            override_duration_days=review.override_duration_days,
        )
    except ServiceError as exc:
        permission_requests_service.raise_http(exc)

    # Log the review action
    if review.action == "approve":
        audit.log_permission_request_approved(
            db=db,
            actor_user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            target_user_id=permission_request.requester_id,
            resource_type=permission_request.resource_type or "cog",
            resource_id=permission_request.resource_id if permission_request.resource_id is not None else permission_request.cog_id,
            permission_request_id=permission_request.id,
            permission_id=permission.id if permission is not None else None,
            permission_level=perm_level,
            metadata={
                "review_notes": review.review_notes,
                "override_level": review.override_level,
                "override_duration_days": review.override_duration_days,
            },
            request=http_request,
        )
    else:  # denied
        audit.log_permission_request_denied(
            db=db,
            actor_user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            target_user_id=permission_request.requester_id,
            resource_type=permission_request.resource_type or "cog",
            resource_id=permission_request.resource_id if permission_request.resource_id is not None else permission_request.cog_id,
            permission_request_id=permission_request.id,
            permission_level=permission_request.requested_level.value,
            metadata={
                "review_notes": review.review_notes,
            },
            request=http_request,
        )

    # Send notification to requester
    requester = permission_requests_service.get_user(db, permission_request.requester_id)

    if requester:
        duration_text = f"{permission_request.requested_duration_days} days" if permission_request.requested_duration_days else "Permanent"

        if review.action == "approve":
            # Get the granted permission level (could be overridden)
            granted_level = review.override_level if review.override_level else permission_request.requested_level.value
            granted_duration = review.override_duration_days if review.override_duration_days is not None else permission_request.requested_duration_days
            granted_duration_text = f"{granted_duration} days" if granted_duration else "Permanent"

            # Calculate expiration date if applicable
            expires_at_text = None
            if granted_duration:
                expires_at_date = datetime.now(timezone.utc) + timedelta(days=granted_duration)
                expires_at_text = expires_at_date.strftime("%Y-%m-%d")

            notification_conf = {
                "notification_type": "approved",
                "recipient_email": requester.email,
                "recipient_name": f"{requester.first_name} {requester.last_name}",
                "reviewer_name": f"{current_user.first_name} {current_user.last_name}",
                "folder_name": _resource_name_for(db, permission_request),
                "granted_level": granted_level,
                "duration": granted_duration_text,
                "expires_at": expires_at_text,
                "review_notes": review.review_notes,
                "folder_url": f"{os.getenv('DASHBOARD_URL', 'http://dashboard.localhost')}/#files",
                "tenant_id": current_user.tenant_id
            }
        else:  # denied
            notification_conf = {
                "notification_type": "denied",
                "recipient_email": requester.email,
                "recipient_name": f"{requester.first_name} {requester.last_name}",
                "reviewer_name": f"{current_user.first_name} {current_user.last_name}",
                "folder_name": _resource_name_for(db, permission_request),
                "requested_level": permission_request.requested_level.value,
                "review_notes": review.review_notes,
                "dashboard_url": f"{os.getenv('DASHBOARD_URL', 'http://dashboard.localhost')}/#permission-requests",
                "tenant_id": current_user.tenant_id
            }

        from utils.email_sender import send_permission_request_email
        background_tasks.add_task(
            send_permission_request_email,
            db=db,
            tenant_id=current_user.tenant_id,
            recipient_email=notification_conf.get("recipient_email", ""),
            recipient_name=notification_conf.get("recipient_name", ""),
            request_details=notification_conf,
        )

    return _request_to_response(db, permission_request)


@router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_permission_request(
        request_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Cancel a pending permission request.

    Only the requester can cancel their own requests.
    """
    try:
        permission_requests_service.cancel_request(db, request_id=request_id, current_user=current_user)
    except ServiceError as exc:
        permission_requests_service.raise_http(exc)

    return
