"""
Permission Templates Endpoints

Allows users to create, manage, and apply reusable permission templates.

Examples of templates:
- "External Consultant": Editor for 30 days, no share permission
- "Temp Viewer": Viewer for 7 days
- "Project Collaborator": Editor for 90 days, can share
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, status, HTTPException, Request
from sqlalchemy.orm import Session
from datetime import datetime
from pydantic import BaseModel, ConfigDict

import models
from dependencies import get_db, get_current_user
from services import permission_templates_service
from services._base import ServiceError


# --- Request/Response Schemas ---

class PermissionTemplateCreate(BaseModel):
    """Request to create a new permission template."""
    name: str
    description: Optional[str] = None
    permission_level: str  # viewer, editor, owner
    duration_days: Optional[int] = None  # None = no expiration

    # Capability overrides (None means use default for level)
    can_upload: Optional[bool] = None
    can_create_subfolders: Optional[bool] = None
    can_move_files: Optional[bool] = None
    can_process: Optional[bool] = None
    can_share: Optional[bool] = None
    can_delete: Optional[bool] = None


class PermissionTemplateUpdate(BaseModel):
    """Request to update an existing template."""
    name: Optional[str] = None
    description: Optional[str] = None
    permission_level: Optional[str] = None
    duration_days: Optional[int] = None
    is_active: Optional[bool] = None

    can_upload: Optional[bool] = None
    can_create_subfolders: Optional[bool] = None
    can_move_files: Optional[bool] = None
    can_process: Optional[bool] = None
    can_share: Optional[bool] = None
    can_delete: Optional[bool] = None


class PermissionTemplateResponse(BaseModel):
    """Response model for a permission template."""
    id: int
    tenant_id: int
    name: str
    description: Optional[str]
    is_active: bool

    permission_level: str
    duration_days: Optional[int]

    can_upload: Optional[bool]
    can_create_subfolders: Optional[bool]
    can_move_files: Optional[bool]
    can_process: Optional[bool]
    can_share: Optional[bool]
    can_delete: Optional[bool]

    times_used: int
    last_used_at: Optional[datetime]

    created_by_user_id: Optional[int]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ApplyTemplateRequest(BaseModel):
    """Request to apply a template to grant permissions."""
    template_id: int
    cog_ids: List[int]  # Folders to grant access to
    user_ids: Optional[List[int]] = None  # Users to grant access
    emails: Optional[List[str]] = None  # External users to invite

    # Optional overrides (will override template values)
    override_duration_days: Optional[int] = None
    invitation_message: Optional[str] = None


class ApplyTemplateResult(BaseModel):
    """Result of applying template to one user/folder combination."""
    cog_id: int
    user_id: Optional[int] = None
    email: Optional[str] = None
    status: str  # "granted", "already_exists", "error"
    error_message: Optional[str] = None
    permission_id: Optional[int] = None


class ApplyTemplateResponse(BaseModel):
    """Response from applying a template."""
    template_id: int
    template_name: str
    total_requested: int
    successful: int
    failed: int
    results: List[ApplyTemplateResult]


# clean-v2-api identity wave: moved from the top-level `/permission-templates`
# to `/permissions/templates` (design.md — "permissions (+ templates,
# resources sub-resources)"). No main.py change needed — this router already
# carries its own full prefix and is mounted with an empty extra-prefix.
router = APIRouter(
    prefix="/permissions/templates",
    tags=["Permission Templates"]
)


_STATUS_BY_CODE = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "bad_request": status.HTTP_400_BAD_REQUEST,
}


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer error to the exact HTTPException shape the
    pre-migration handlers raised inline (plain string `detail`, no envelope)."""
    raise HTTPException(
        status_code=_STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.message,
    )


@router.get("", response_model=List[PermissionTemplateResponse])
def list_permission_templates(
        include_inactive: bool = False,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    List all permission templates for the current tenant.

    By default, only returns active templates.
    """
    return permission_templates_service.list_templates(
        db, tenant_id=current_user.tenant_id, include_inactive=include_inactive
    )


@router.get("/{template_id}", response_model=PermissionTemplateResponse)
def get_permission_template(
        template_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """Get a specific permission template by ID."""
    try:
        return permission_templates_service.get_template(
            db, template_id, tenant_id=current_user.tenant_id
        )
    except ServiceError as exc:
        _raise_http(exc)


@router.post("", response_model=PermissionTemplateResponse, status_code=status.HTTP_201_CREATED)
def create_permission_template(
        request: PermissionTemplateCreate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Create a new permission template.

    Templates are tenant-specific and can be reused to quickly grant
    consistent permissions.
    """
    try:
        return permission_templates_service.create_template(
            db,
            tenant_id=current_user.tenant_id,
            created_by_user_id=current_user.id,
            data=request.model_dump(),
        )
    except ServiceError as exc:
        _raise_http(exc)


@router.put("/{template_id}", response_model=PermissionTemplateResponse)
def update_permission_template(
        template_id: int,
        request: PermissionTemplateUpdate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Update an existing permission template.

    Only the creator or tenant admins should be able to update templates.
    """
    # TODO: Add permission check (only creator or admin can update)
    update_data = request.model_dump(exclude_unset=True)
    try:
        return permission_templates_service.update_template(
            db, template_id, tenant_id=current_user.tenant_id, update_data=update_data
        )
    except ServiceError as exc:
        _raise_http(exc)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_permission_template(
        template_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Delete a permission template.

    Only the creator or tenant admins should be able to delete templates.
    """
    # TODO: Add permission check (only creator or admin can delete)
    try:
        permission_templates_service.delete_template(db, template_id, tenant_id=current_user.tenant_id)
    except ServiceError as exc:
        _raise_http(exc)

    return


@router.post("/{template_id}/apply", response_model=ApplyTemplateResponse)
def apply_permission_template(
        template_id: int,
        request: ApplyTemplateRequest,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Apply a permission template to grant permissions.

    This is a convenience endpoint that combines template retrieval
    and bulk permission granting.

    User must have 'can_share' permission on all target folders.
    """
    try:
        result = permission_templates_service.apply_template(
            db,
            template_id,
            tenant_id=current_user.tenant_id,
            current_user_id=current_user.id,
            cog_ids=request.cog_ids,
            user_ids=request.user_ids,
            emails=request.emails,
            override_duration_days=request.override_duration_days,
            invitation_message=request.invitation_message,
            http_request=http_request,
        )
    except ServiceError as exc:
        _raise_http(exc)

    return ApplyTemplateResponse(**result)


@router.post("/{template_id}/duplicate", response_model=PermissionTemplateResponse, status_code=status.HTTP_201_CREATED)
def duplicate_permission_template(
        template_id: int,
        new_name: str,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(get_current_user)
):
    """
    Duplicate an existing template with a new name.

    Useful for creating variations of existing templates.
    """
    try:
        return permission_templates_service.duplicate_template(
            db,
            template_id,
            tenant_id=current_user.tenant_id,
            created_by_user_id=current_user.id,
            new_name=new_name,
        )
    except ServiceError as exc:
        _raise_http(exc)
