"""IAM permissions domain — role presets + per-resource permission grant/revoke.

Gate 2b migration: data access moved to `crud/resource_perms_iam.py` (the only
place `db.query` runs for this sub-module) and policy/orchestration moved to
`services/resource_perms_iam_service.py`. Handlers stay thin HTTP shims —
auth, request/response shaping, and mapping a `ServiceError` to the exact
`HTTPException` shape this router always raised inline (plain string
`detail`, same status code).
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from resource_permissions import ROLE_PRESETS
import services.resource_perms_iam_service as service
from services._base import ServiceError
from services import approval_gate

from . import GrantRequest, RolePresetRequest, ResourcePermissionResponse, RolePresetInfo

router = APIRouter()

_STATUS_BY_CODE = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "bad_request": status.HTTP_400_BAD_REQUEST,
}


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer error to the exact `HTTPException` shape this
    router always raised inline (plain string `detail`)."""
    raise HTTPException(
        status_code=_STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.message,
    )


# =========================================================================
# Role Presets
# =========================================================================

@router.get("/presets", response_model=List[RolePresetInfo])
def list_role_presets(
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """List available role presets."""
    result = []
    for name, preset in ROLE_PRESETS.items():
        if preset["capabilities"] == "all":
            types = [rt.value for rt in models.ResourceType]
        else:
            types = [rt.value for rt in preset["capabilities"].keys()]
        result.append(RolePresetInfo(
            name=name,
            display_name=preset["display_name"],
            description=preset["description"],
            resource_types=types,
        ))
    return result


@router.post("/presets/apply")
def apply_preset(
    request: RolePresetRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Apply a role preset to a user (admin / role-assigner only).

    Bug #5 fix: the target user is now resolved by id FIRST, then tenant
    isolation is applied. Previously the lookup filtered on
    ``current_user.tenant_id``, so a platform admin operating on another
    tenant's user via the user-management tenant selector always got a
    spurious 404 "User not found" (the target lives in the selected tenant,
    not the admin's own). The preset is now also applied to the TARGET
    user's tenant, and the assignment emits an ISO 27001 AU-2 audit row.

    GSEC FX.163 p2 [B] (Q150=A — segregation of duties): this is the sibling
    role surface of PUT /users/{id}/assign-role — a preset rewrites the user's
    whole capability set, so while the TARGET tenant's
    ``approvals.require_second_approver`` switch is ON (or a gov/DoD / banking
    profile forces it) the preset does NOT run: the call returns 202
    ``{"status": "awaiting_approval", "approval_id": ...}`` and the preset is
    applied only when a DIFFERENT tenant admin approves that row
    (POST /v1/pending-approvals/{approval_id}/approve). Switch OFF = today's
    behaviour, byte for byte.
    """
    awaiting = approval_gate.gate_iam_preset_apply(
        db, caller=current_user, user_id=request.user_id,
        preset=request.preset)
    if awaiting:
        return approval_gate.awaiting_response(awaiting)
    try:
        return service.apply_role_preset(
            db, preset=request.preset, user_id=request.user_id, current_user=current_user,
        )
    except ServiceError as exc:
        _raise_http(exc)


# =========================================================================
# Per-resource permission CRUD
# =========================================================================

@router.get("/resource/{resource_type}/{resource_id}", response_model=List[ResourcePermissionResponse])
def list_resource_permissions(
    resource_type: str,
    resource_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """List all permissions on a specific resource."""
    try:
        rows = service.list_resource_permissions(
            db, resource_type=resource_type, resource_id=resource_id, current_user=current_user,
        )
    except ServiceError as exc:
        _raise_http(exc)
    return [ResourcePermissionResponse(**row) for row in rows]


@router.post("/grant")
def grant_permission(
    request: GrantRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Grant a capability on a resource to a user."""
    try:
        return service.grant_permission(
            db,
            resource_type=request.resource_type,
            capability_name=request.capability_name,
            resource_id=request.resource_id,
            user_id=request.user_id,
            current_user=current_user,
        )
    except ServiceError as exc:
        _raise_http(exc)
