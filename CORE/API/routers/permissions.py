# In Main/routers/permissions.py

from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, status, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

import schemas, models
from dependencies import get_db, require_active_and_onboarded_user
from permissions import (
    can_user_share_cog,
    is_permission_expired,
    get_permission_summary,
    get_user_permission_on_cog,
    get_inherited_permissions,
    get_effective_permissions_for_cog,
)
from models import PermissionTargetType
from cog_grants import (  # Gate 30: sharing-grant read model
    list_cog_grants, get_cog_grant_by_id)
import crud.permissions_router as crud
import services.permissions_router_service as service
from services._base import ServiceError

# Gate 2b: data access -> crud/permissions_router.py (only place db.query
# lives for this domain); policy/orchestration -> services/permissions_router_service.py.
# `can_user_share_cog` / `get_effective_permissions_for_cog` /
# `get_inherited_permissions` / `get_user_permission_on_cog` stay imported
# HERE (not moved into the service) because tests patch them at
# `routers.permissions.<name>` — see services/permissions_router_service.py's
# module docstring.

_STATUS_BY_CODE = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "bad_request": status.HTTP_400_BAD_REQUEST,
    "unprocessable_entity": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "conflict": status.HTTP_409_CONFLICT,
}


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer error to the exact `HTTPException` shape this
    router always raised inline (plain string `detail`)."""
    raise HTTPException(
        status_code=_STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.message,
    )


router = APIRouter(
    tags=["Permissions"],
    dependencies=[Depends(require_active_and_onboarded_user)]
)


@router.get("/cogs/{cog_id}", response_model=List[schemas.Permission])
def list_permissions_for_cog(
        cog_id: int,
        include_inherited: bool = False,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Lists permission grants for a specific cog (folder or file).

    Args:
        cog_id: The cog ID (folder or file)
        include_inherited: If True, includes permissions inherited from parent folders

    Requires 'can_share' permission on the cog.
    """
    # Check if user has permission to view permissions (can_share)
    if not can_user_share_cog(db, current_user.id, cog_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to view permissions for this item"
        )

    if include_inherited:
        # Get all effective permissions (direct + inherited)
        permissions = get_effective_permissions_for_cog(db, cog_id)
    else:
        # Gate 30: direct sharing grants (reconstructed from ResourcePermission).
        permissions = list_cog_grants(db, tenant_id=current_user.tenant_id,
                                      cog_ids=[cog_id])

    # Convert to response schema with effective capabilities (enrichment +
    # data access in services/permissions_router_service.py).
    return [
        schemas.Permission(**row)
        for row in service.build_cog_permission_list(db, permissions, cog_id)
    ]


@router.get("/cogs/{cog_id}/inherited", response_model=List[schemas.Permission])
def list_inherited_permissions_for_cog(
        cog_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Lists permissions inherited from parent folders.
    Requires 'can_share' permission on the cog.
    """
    # Check if user has permission to view permissions
    if not can_user_share_cog(db, current_user.id, cog_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to view permissions for this item"
        )

    # Get inherited permissions
    inherited = get_inherited_permissions(db, cog_id)

    # Convert to response schema
    # Note: get_permission_summary includes 'level', so we don't pass it separately
    return [
        schemas.Permission(
            id=perm.id,
            target_type=perm.target_type.value,
            target_id=perm.target_id,
            user_id=perm.user_id,
            team_id=perm.team_id,
            keycloak_group_id=perm.keycloak_group_id,
            **get_permission_summary(perm)
        )
        for perm in inherited
    ]


@router.post("/cogs/{cog_id}", response_model=schemas.Permission, status_code=status.HTTP_201_CREATED)
def grant_permission_to_cog(
        cog_id: int,
        permission: schemas.PermissionCreate,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Grants a user, team, or Keycloak group a specific permission level on a cog (folder or file).
    Requires 'can_share' permission on the cog.

    Capability flags can be optionally overridden from level defaults.
    """
    # Check if user has permission to share
    if not can_user_share_cog(db, current_user.id, cog_id):
        # data-files-launch-bulletproof §0.5 — emit cross-tenant audit
        # if a platform admin tried to grant on a cog in another tenant.
        service.maybe_log_cross_tenant_cog_share_blocked(db, current_user, cog_id, http_request)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to share this item"
        )

    try:
        result = service.grant_cog_permission(db, current_user, cog_id, permission, http_request)
    except ServiceError as exc:
        _raise_http(exc)

    # Return with effective capabilities
    return schemas.Permission(**result)


# ---------------------------------------------------------------------------
# WORKFLOW SHARING
# ---------------------------------------------------------------------------
# Files/cogs share via the level-based Permission model above. Workflows are a
# capability-based resource (ResourcePermission rows per capability), so sharing
# a workflow means granting the capability SET for a level — the same mapping
# the permission-request approval path uses (routers/permission_requests.py).
# There was previously NO endpoint to grant a workflow to a user, so the
# WorkflowBuilder "share" never actually granted access: a recipient could see a
# workflow in the tenant-wide list but got 403 on open (needs "view") and 403 on
# run (needs "execute"). This endpoint is the missing grant path.

# Capability-set-per-level mapping now lives in
# services.permissions_router_service.WORKFLOW_CAPS_BY_LEVEL (moved with the
# rest of the grant policy, Gate 2b).


class WorkflowShareRequest(BaseModel):
    """Share a workflow with a single user at a permission level."""
    user_id: int
    level: str = "editor"
    expires_at: Optional[datetime] = None


@router.post("/road/{workflow_id}", tags=["The Road"], status_code=status.HTTP_201_CREATED)
def grant_permission_to_workflow(
        workflow_id: int,
        body: WorkflowShareRequest,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Grant a user access to a workflow (see + open + run).

    Requires the "share" capability on the workflow (the owner has it via derived
    ownership; a user shared at "owner" level also holds it). Granting at "owner"
    level additionally requires "manage_permissions". Tenant-isolated: both the
    workflow and the target user must belong to the sharer's tenant.
    """
    try:
        return service.grant_workflow_permission(db, current_user, workflow_id, body, http_request)
    except ServiceError as exc:
        _raise_http(exc)


@router.patch("/{permission_id}", response_model=schemas.Permission)
def update_permission(
        permission_id: int,
        permission_update: schemas.PermissionUpdate,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Updates a permission's level or capability overrides.
    Requires 'can_share' permission on the target cog.
    """
    # Get the permission (tenant-scoped, exclude soft-deleted)
    perm = get_cog_grant_by_id(db, permission_id)  # Gate 30: grant by read-model id
    if perm is not None and perm.tenant_id != current_user.tenant_id:
        perm = None
    if not perm:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permission not found"
        )

    # Check if user has permission to modify permissions
    if perm.target_type == PermissionTargetType.COG:
        if not can_user_share_cog(db, current_user.id, perm.target_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to modify permissions for this item"
            )
    else:
        # For workflows, we'd check workflow permissions here
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Workflow permissions not yet implemented"
        )

    try:
        result = service.apply_cog_permission_update(db, current_user, perm, permission_update, http_request)
    except ServiceError as exc:
        _raise_http(exc)

    # Return with effective capabilities
    return schemas.Permission(**result)


@router.delete("/{permission_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_permission(
        permission_id: int,
        http_request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Revokes a permission grant by its ID.
    Requires 'can_share' permission on the target resource.

    Uses soft-delete for ISO 27001 compliance (audit trail preservation).
    """
    # Get the permission (tenant-scoped, exclude already soft-deleted)
    perm = get_cog_grant_by_id(db, permission_id)  # Gate 30: grant by read-model id
    if perm is not None and perm.tenant_id != current_user.tenant_id:
        perm = None
    if not perm:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permission not found"
        )

    # Check if user has permission to revoke permissions
    if perm.target_type == PermissionTargetType.COG:
        if not can_user_share_cog(db, current_user.id, perm.target_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to revoke permissions for this item"
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Workflow permissions not yet implemented"
        )

    service.revoke_cog_permission(db, current_user, perm, http_request)
    return


@router.get("/my-permissions/cogs/{cog_id}", response_model=schemas.Permission)
def get_my_permission_on_cog(
        cog_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get the current user's effective permission on a specific cog (folder or file).
    Useful for the frontend to determine what actions the user can perform.
    """
    # Check if user is the owner (tenant-scoped, exclude soft-deleted)
    cog = crud.get_cog_in_tenant(db, cog_id, current_user.tenant_id)
    if not cog:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item not found"
        )

    if cog.owner_id == current_user.id:
        # Owners have full permissions
        return schemas.Permission(**service.owner_permission_view(cog, current_user))

    # Get user's permission
    perm = get_user_permission_on_cog(db, current_user.id, cog_id)
    if not perm:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this item"
        )

    # Return with effective capabilities
    # Note: get_permission_summary includes 'level', so we don't pass it separately
    return schemas.Permission(
        id=perm.id,
        target_type=perm.target_type.value,
        target_id=perm.target_id,
        user_id=perm.user_id,
        team_id=perm.team_id,
        keycloak_group_id=perm.keycloak_group_id,
        **get_permission_summary(perm)
    )
