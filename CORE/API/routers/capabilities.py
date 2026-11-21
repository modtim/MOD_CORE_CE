"""
Granular Capabilities and Resource Permissions API

This module provides endpoints for managing the granular permission system:
- Capabilities: System-defined actions that can be granted on resources
- Resource Permissions: Grants of capabilities to users/teams/roles on specific resources
"""

from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_
from collections import defaultdict

import schemas
import models
from models import ResourceType
from dependencies import get_db, require_active_and_onboarded_user
from services._base import ServiceError
import services.capabilities_service as capabilities_service
# FIX-PAID-GAPS (owner Q5=B): the three role-capability WRITE routes further
# down reuse the paid RBAC-management gate that routers/rbac.py already
# applies to its five roles routes — no new mechanism. Their bodies are the
# only place this module is referenced, and they sit inside the
# open-core/manifest.yaml `scrub:` cut, so the CE extract keeps neither the
# routes nor any attribute access on this module; the import itself stays and
# resolves (services/rbac_service.py ships in CE as its overlay).
from pydantic import BaseModel, ConfigDict


class MessageResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: Optional[str] = None


class ResourceAccessReviewResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    resource_type: Optional[str] = None
    resource_id: Optional[int] = None
    exhaustive: Optional[bool] = None
    principals: Optional[list] = None
    reconstruction: Optional[object] = None


class UserEffectiveAccessResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    user_id: Optional[int] = None
    access: Optional[object] = None


class OwnershipTransferResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    resource_type: Optional[str] = None
    resource_id: Optional[int] = None
    old_owner: Optional[list] = None
    new_owner: Optional[list] = None
    old_owner_user_id: Optional[int] = None
    new_owner_user_id: Optional[int] = None

router = APIRouter(
    tags=["Capabilities"],
    dependencies=[Depends(require_active_and_onboarded_user)],
    responses={404: {"description": "Not found"}},
)


# Domain-local ServiceError -> HTTPException mapping (openspec:
# stack-training-and-prewarm Gate 2). Every status code/message below is
# copied verbatim from the pre-migration inline handlers so the runtime
# contract is unchanged; api_conventions.ErrorEnvelope is not installed yet
# (see api_conventions.py docstring), so `detail` stays a plain string here,
# matching what every endpoint in this file already returned.
_STATUS_BY_CODE = {
    "not_found": 404,
    "bad_request": 400,
    "forbidden": 403,
    "conflict": 409,
    "unprocessable_entity": 422,
}


def _raise_http(exc: ServiceError):
    raise HTTPException(status_code=_STATUS_BY_CODE.get(exc.code, 400), detail=exc.message)


def _permission_to_response(
    perm: models.ResourcePermission, capability: Optional[models.Capability] = None
) -> schemas.ResourcePermissionResponse:
    """Shape a `ResourcePermission` row (+ its capability) into the response
    schema. Pure presentation, no data access — shared by the four handlers
    that return this shape, exactly reproducing each one's original inline
    dict construction."""
    cap = capability if capability is not None else perm.capability
    return schemas.ResourcePermissionResponse(
        id=perm.id,
        capability_id=perm.capability_id,
        capability_name=cap.name if cap else None,
        capability_display_name=cap.display_name if cap else None,
        resource_type=perm.resource_type,
        resource_id=perm.resource_id,
        user_id=perm.user_id,
        team_id=perm.team_id,
        role_id=perm.role_id,
        conditions=perm.conditions,
        expires_at=perm.expires_at,
        granted_by_user_id=perm.granted_by_user_id,
        granted_at=perm.granted_at,
        is_active=perm.is_active,
    )


# --- Capability Endpoints (Read-only, capabilities are seeded) ---

@router.get("/capabilities", response_model=List[schemas.CapabilityResponse])
def list_capabilities(
    resource_type: Optional[ResourceType] = None,
    category: Optional[str] = None,
    skip: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    List all available capabilities.

    Optionally filter by resource_type or category.
    """
    return capabilities_service.list_capabilities(
        db, resource_type=resource_type, category=category, skip=skip, limit=limit
    )


@router.get("/capabilities/grouped", response_model=List[schemas.CapabilityGrouped])
def list_capabilities_grouped(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    List all capabilities grouped by resource type and category.

    This is the primary endpoint for the UI to display capability selections.
    """
    return capabilities_service.list_capabilities_grouped(db)


@router.get("/capabilities/{capability_id}", response_model=schemas.CapabilityResponse)
def get_capability(
    capability_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Get a specific capability by ID."""
    try:
        return capabilities_service.get_capability(db, capability_id)
    except ServiceError as exc:
        _raise_http(exc)


# --- Resource Permission Endpoints ---

@router.get("/resource-permissions", response_model=List[schemas.ResourcePermissionResponse])
def list_resource_permissions(
    resource_type: Optional[ResourceType] = None,
    resource_id: Optional[int] = None,
    user_id: Optional[int] = None,
    team_id: Optional[int] = None,
    role_id: Optional[int] = None,
    include_expired: bool = False,
    skip: int = 0,
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    List resource permissions with optional filters.

    Platform admins can view all permissions.
    Regular users can only view permissions in their tenant.

    Pagination: when listing the grants of ONE resource (a `resource_id` filter
    is supplied — the share dialog's case) the FULL set of grants is returned by
    default so the UI can render every share, not a truncated first page. When
    browsing across resources without a `resource_id`, results default to a
    100-row page. Either way an explicit `limit` is honored. A hard safety cap
    (`_MAX_ROWS`) bounds any single response.
    """
    permissions = capabilities_service.list_resource_permissions(
        db,
        current_user=current_user,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=user_id,
        team_id=team_id,
        role_id=role_id,
        include_expired=include_expired,
        skip=skip,
        limit=limit,
    )
    return [_permission_to_response(perm) for perm in permissions]


@router.post("/resource-permissions", response_model=schemas.ResourcePermissionResponse, status_code=status.HTTP_201_CREATED)
def create_resource_permission(
    permission: schemas.ResourcePermissionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Create a new resource permission.

    Grants a specific capability to a user, team, or role on a resource.
    """
    try:
        db_permission, capability = capabilities_service.create_resource_permission(
            db, permission, current_user
        )
    except ServiceError as exc:
        _raise_http(exc)
    return _permission_to_response(db_permission, capability)


@router.post("/resource-permissions/bulk", response_model=List[schemas.ResourcePermissionResponse], status_code=status.HTTP_201_CREATED)
def create_resource_permissions_bulk(
    bulk_request: schemas.ResourcePermissionBulkCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Create multiple resource permissions at once.

    Useful for granting multiple capabilities to a user/team/role at once.
    """
    try:
        created = capabilities_service.create_resource_permissions_bulk(
            db, bulk_request, current_user
        )
    except ServiceError as exc:
        _raise_http(exc)
    return [_permission_to_response(perm, cap) for perm, cap in created]


@router.patch("/resource-permissions/{permission_id}", response_model=schemas.ResourcePermissionResponse)
def update_resource_permission(
    permission_id: int,
    update: schemas.ResourcePermissionUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Update a resource permission (conditions or expiration only)."""
    try:
        permission = capabilities_service.update_resource_permission(
            db, permission_id, update, current_user
        )
    except ServiceError as exc:
        _raise_http(exc)
    return _permission_to_response(permission)


@router.delete("/resource-permissions/{permission_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_resource_permission(
    permission_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):

    """Revoke (soft-delete) a resource permission.

    AUTHORIZATION: revoking a grant is a privileged action reserved to whoever
    manages the resource. A caller may revoke a grant ONLY when they hold
    "manage_permissions"/"share" on the grant's resource (the resource owner does,
    via derived ownership).

    Rule correction: a user must NOT be able to remove their OWN access when
    someone else granted it, and a grant's original creator must NOT be able to
    revoke it once they no longer manage the resource. Both the self-removal and
    the grant-creator allowances are therefore gone — only a resource manager
    (owner / manage_permissions / share holder) can revoke. Without this any
    tenant user could revoke access (their own, or by prior escalation another's)
    on a row that is only tenant-scoped.
    """
    try:
        capabilities_service.delete_resource_permission(db, permission_id, current_user)
    except ServiceError as exc:
        _raise_http(exc)
    return None


# --- Role Capability Management ---

@router.get("/roles/{role_id}/capabilities", response_model=List[schemas.CapabilityResponse])
def get_role_capabilities(
    role_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """Get all capabilities assigned to a role."""
    try:
        return capabilities_service.get_role_capabilities(db, role_id, current_user)
    except ServiceError as exc:
        _raise_http(exc)


# FIX-PAID-GAPS (owner Q5=B) — the three role-capability WRITE routes below
# rewrite what a role grants, i.e. the same paid operation PATCH
# /rbac/roles/{id} performs, so on top of their ROLE:edit capability check
# they now call the SAME paid RBAC-management gate routers/rbac.py already
# uses on its five roles routes (unentitled tenant -> 402 upgrade message,
# code `rbac_custom_required`; no new mechanism, same message + code).
#
# Ordering: the ROLE:edit capability check is a route dependency, so it runs
# before this body check — a caller missing both gets the 403, a caller who
# holds ROLE:edit but is not entitled gets the 402. Both deny the write.
# routers/rbac.py answers 402-first because it declares its gate as a
# dependency BEFORE require_capability; mirroring that here would put a new
# gate name in the CE extract (the open-core entitlement ratchet lists every
# such name), and these routes are cut from CE outright, so the body check is
# the shape that keeps the CE tree byte-identical.
#
# Everything from the decorator below to the "# --- Permission Check Endpoints
# ---" marker is inside the open-core/manifest.yaml `scrub:` cut, so the CE
# extract keeps neither the routes nor any reference to that gate.
# Role capability writes (PUT/POST/DELETE) are not part of Community Edition:
# the built-in roles are fixed. Reading a role's capabilities (GET above) stays.


# --- Permission Check Endpoints ---

@router.post("/permissions/check", response_model=schemas.PermissionCheckResponse)
def check_permission(
    request: schemas.PermissionCheckRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Check if the current user has a specific capability on a resource.

    Returns whether the user has the permission and how it was granted.
    """
    return capabilities_service.check_permission(db, request, current_user)


@router.get("/users/{user_id}/effective-permissions", response_model=List[schemas.EffectivePermissions])
def get_user_effective_permissions(
    user_id: int,
    resource_type: Optional[ResourceType] = None,
    resource_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Get all effective permissions for a user.

    This aggregates permissions from:
    - Direct user permissions
    - Role capabilities
    - Team permissions
    """
    try:
        return capabilities_service.get_user_effective_permissions(
            db, user_id, resource_type, resource_id, current_user
        )
    except ServiceError as exc:
        _raise_http(exc)


# ---------------------------------------------------------------------------
# authz-derived-ownership DO-2 — the effective-access review surface. WHO can
# reach a resource / WHAT a user can reach, enforcement-equivalent, for
# ISO/CMMC/FedRAMP/TPN access reviews. Platform-admin only; tenant-isolated.
# ---------------------------------------------------------------------------
@router.get("/resources/{resource_type}/{resource_id}/access-review", response_model=ResourceAccessReviewResponse)
def resource_access_review(
    resource_type: ResourceType,
    resource_id: int,
    exhaustive: bool = Query(False, description="Check every user in the tenant (audit proof)"),
    as_of: Optional[str] = Query(None, description="ISO timestamp — reconstruct access AS OF this past time (DO-5.2)"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):

    """WHO can access this resource — owner ∪ folder-inherited ∪ direct ∪ team ∪
    role — each confirmed through the same engine live enforcement uses. Set
    `exhaustive=true` for the check-every-user proof; set `as_of=<ISO>` for a
    point-in-time reconstruction (owner + grants in effect at that past time)."""
    try:
        return capabilities_service.resource_access_review(
            db, resource_type, resource_id,
            exhaustive=exhaustive, as_of=as_of, current_user=current_user,
        )
    except ServiceError as exc:
        _raise_http(exc)


@router.get("/users/{user_id}/effective-access", response_model=UserEffectiveAccessResponse)
def user_effective_access(
    user_id: int,
    resource_type: Optional[ResourceType] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):

    """WHAT a user can reach — owned ∪ folder-inherited ∪ granted — confirmed
    through the enforcement engine. Platform-admin, or the user themselves."""
    try:
        return capabilities_service.user_effective_access(db, user_id, resource_type, current_user)
    except ServiceError as exc:
        _raise_http(exc)


from pydantic import BaseModel as _BaseModel  # noqa: E402


class TransferOwnershipBody(_BaseModel):
    new_owner_user_id: Optional[int] = None
    new_owner_team_id: Optional[int] = None  # DO-4: shared-drive / team ownership


@router.post("/resources/{resource_type}/{resource_id}/transfer-ownership", status_code=status.HTTP_200_OK, response_model=OwnershipTransferResponse)
def transfer_resource_ownership(
    resource_type: ResourceType,
    resource_id: int,
    body: TransferOwnershipBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):

    """authz-derived-ownership DO-3/DO-4 — reassign a resource's owner to a USER
    or a TEAM (shared-drive model).

    Permitted to the CURRENT owner (Drive's "Make owner") or a platform admin.
    Tenant-isolated; audited. Ownership is an authoritative pointer/row, so the
    derived-owner rule moves all owner access to the new owner atomically — for
    a team, to every member of that team.
    """
    try:
        return capabilities_service.transfer_resource_ownership(
            db, resource_type, resource_id, body, request, current_user
        )
    except ServiceError as exc:
        _raise_http(exc)
