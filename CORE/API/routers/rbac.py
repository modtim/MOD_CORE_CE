from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import schemas, models
from dependencies import (get_db, require_active_and_onboarded_user,
                          require_capability, require_fresh_auth)
from services import rbac_service
from services import approval_gate
from services._base import ConflictError, NotFoundError, ServiceError

router = APIRouter(
    tags=["RBAC"],
    dependencies=[Depends(require_active_and_onboarded_user)],
    responses={404: {"description": "Not found"}},
)


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer error to the HTTPException shape the handlers raise."""
    if isinstance(exc, rbac_service.ForbiddenError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message)
    if isinstance(exc, ConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message)
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=404, detail=exc.message)
    raise HTTPException(status_code=400, detail=exc.message)


@router.get("/roles", response_model=List[schemas.CustomRole])
def read_custom_roles(
    skip: int = 0,
    limit: int = 100,
    tenant_id: int = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_capability(models.ResourceType.ROLE, "view"))
):
    """
    Retrieve the built-in system roles for the current user's tenant
    (Admin, Standard User, Guest, External Collaborator).
    """
    try:
        return rbac_service.list_roles(db, current_user, tenant_id=tenant_id, skip=skip, limit=limit)
    except ServiceError as exc:
        _raise_http(exc)


@router.put("/users/{user_id}/assign-role", response_model=schemas.User)
def assign_role_to_user(
    user_id: int,
    role_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_capability(models.ResourceType.USER, "change_role")),
    fresh: models.User = Depends(require_fresh_auth)  # GY.C4b re-auth
):
    """
    Assign a role to a user.

    A platform admin may assign a role to a user in ANY tenant — this is what
    the user-management tenant selector needs; the role is looked up in the
    TARGET user's tenant, and the assignment is audited against that tenant.
    A non-platform-admin may only assign within their own tenant (and must hold
    USER:change_role, enforced by the dependency); a cross-tenant target is
    reported as a plain 404 so tenant membership isn't leaked.

    Segregation of duties: while
    ``approvals.require_second_approver`` is ON for the TARGET tenant, the
    assignment does NOT run — the call returns 202 ``{"status":
    "awaiting_approval", "approval_id": ...}`` and the role is applied only
    when a DIFFERENT tenant admin approves that row
    (POST /v1/pending-approvals/{id}/approve). Switch OFF = the assignment
    runs immediately.
    """
    awaiting = approval_gate.gate_role_grant(
        db, caller=current_user, user_id=user_id, role_id=role_id)
    if awaiting:
        return approval_gate.awaiting_response(awaiting)
    try:
        return rbac_service.assign_role(db, current_user, user_id=user_id, role_id=role_id)
    except ServiceError as exc:
        _raise_http(exc)
