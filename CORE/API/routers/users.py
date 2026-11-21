# In Main/routers/users.py

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from keycloak_auth import keycloak_admin_client
import os
import logging

import models, schemas, crud
from dependencies import get_db, require_active_and_onboarded_user, require_capability
from services import users_router_service as service
from services._base import ServiceError

# Setup logging
logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data)
from services.debug_mode import is_debug_mode

router = APIRouter(
    tags=["Users"],
    dependencies=[Depends(require_active_and_onboarded_user)],
    responses={404: {"description": "Not found"}},
)

# Gate 2b: data access -> crud/users_router.py (new admin/include-deleted
# lookups) + the existing crud/user_cud.py + crud/tenant_cud.py functions
# (reused via the `crud` package, unchanged); policy/orchestration ->
# services/users_router_service.py. See that module's docstring for the
# 404-vs-403 reasoning and which patch sites are preserved.


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer error to the plain-string-detail HTTPException
    shape this router returned pre-migration (contract-snapshot byte-
    identical: no error-envelope dict wrapping)."""
    status_code = {
        "not_found": status.HTTP_404_NOT_FOUND,
        "forbidden": status.HTTP_403_FORBIDDEN,
        "conflict": status.HTTP_409_CONFLICT,
        "bad_request": status.HTTP_400_BAD_REQUEST,
    }.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    raise HTTPException(status_code=status_code, detail=exc.message)


@router.get("/", response_model=List[schemas.User])
def read_users(
        tenant_id: int = None,
        skip: int = 0,
        limit: int = 100,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_capability(models.ResourceType.USER, "view"))
):
    """
    Retrieve all users within a tenant.

    - Regular users: Can only see users in their own tenant
    - Platform admins: Can specify tenant_id to view users from any tenant
    """
    try:
        return service.list_users(db, current_user, tenant_id=tenant_id, skip=skip, limit=limit)
    except ServiceError as exc:
        _raise_http(exc)


# clean-v2-api identity wave: moved to routers/tenants.py as `GET /tenants`
# (routers.tenants.list_tenants, same services.users_router_service.list_tenants
# call). Decorator removed; function kept for backward-compat imports.
def list_all_tenants(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    List all tenants in the system (Platform Admin only).

    Regular users will receive a 403 Forbidden error.
    """
    try:
        return service.list_tenants(db, current_user)
    except ServiceError as exc:
        _raise_http(exc)


# clean-v2-api identity wave: moved to routers/tenants.py as
# `DELETE /tenants/{tenant_id}` (routers.tenants.delete_tenant, same
# services.users_router_service.delete_tenant call). Decorator removed;
# function kept for backward-compat imports.
def delete_tenant_endpoint(
    tenant_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Soft-delete a tenant (Platform Admin only).

    Scrubs the tenant name, stamps `deleted_at`, and cascade-soft-deletes
    every user that belongs to the tenant (login disabled, PII scrubbed,
    row kept for audit attribution). ISO 27001 compliant — no hard delete.
    """
    try:
        service.delete_tenant(db, current_user, tenant_id)
    except ServiceError as exc:
        _raise_http(exc)
    return


# clean-v2-api identity wave: moved to routers/tenants.py as `POST /tenants`
# (routers.tenants.create_tenant, same services.users_router_service.create_tenant
# call). Decorator removed; function kept for backward-compat imports.
def create_tenant(
    tenant_data: schemas.TenantCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Create a new tenant organization (Platform Admin only).

    This will create a new tenant with default roles:
    - Admin (all permissions)
    - Standard User (most permissions)
    - Guest (read-only)
    - External Collaborator (limited)

    After creating the tenant, you can invite users to it.
    """
    try:
        return service.create_tenant(db, current_user, tenant_data)
    except ServiceError as exc:
        _raise_http(exc)


@router.get("/{user_id:int}", response_model=schemas.User)
def read_user(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_capability(models.ResourceType.USER, "view_details"))
):
    """
    Retrieve a specific user from the current user's tenant.
    """
    try:
        return service.get_user(db, current_user, user_id)
    except ServiceError as exc:
        _raise_http(exc)


@router.patch("/{user_id}", response_model=schemas.User)
def update_user(
        user_id: int,
        user: schemas.UserUpdate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_capability(models.ResourceType.USER, "edit_profile"))
):
    """
    Update a user's role or status. The user must be in the same tenant.
    """
    try:
        return service.update_user(db, current_user, user_id, user)
    except ServiceError as exc:
        _raise_http(exc)


class ExportControlUpdate(BaseModel):
    """GY.V2b — the US-person attribute on a user."""
    us_person: bool


class ExportControlStatus(BaseModel):
    user_id: int
    us_person: bool


@router.get("/{user_id}/export-control", response_model=ExportControlStatus)
def get_user_export_control(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """GY.V2b — read the US-person attribute of a user in this tenant."""
    # 1788h CE carve: data_classification is paid/denied. CE has no export-control
    # policy, so the read falls back to the module's own fail-closed default
    # (absent flag = False) — the read path itself is unchanged.
    from utils.paid_import import optional_paid
    _dc = optional_paid("services.data_classification")
    if current_user.parent_user_id is not None and not current_user.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a tenant admin or platform admin may read "
                   "export-control attributes",
        )
    target = crud.get_user(db, user_id)
    if target is None or target.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if not current_user.is_platform_admin and target.tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot read export-control attributes of users in other tenants",
        )
    return {"user_id": user_id,
            "us_person": (_dc.is_us_person(target, db) if _dc is not None else False)}


@router.patch("/{user_id}/export-control", response_model=ExportControlStatus)
def set_user_export_control(
        user_id: int,
        body: ExportControlUpdate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """GY.V2b — set/clear the US-person attribute (tenant admin only).

    The attribute gates read/download/run access to EAR/ITAR-classified
    assets (and CUI when compliance.cui_requires_us_person is on).
    Stored on the user's existing JSON settings row (UserSetting, key
    ``export_control``) — no users-schema change. Audit: US_PERSON_FLAG_SET.
    """
    # 1788h CE carve: data_classification is paid/denied. CE keeps the SAME
    # admin predicate inline (platform-admin bypass + tenant parent user, the
    # module's own definition), so the endpoint stays admin-only without the
    # paid module; the paid US-person write (and its audit row) is skipped —
    # CE ships no export-control policy, so there is nothing to flag.
    from utils.paid_import import optional_paid
    _dc = optional_paid("services.data_classification")
    is_admin_user = (
        _dc.is_admin_user
        if _dc is not None else
        lambda u: bool(u) and (getattr(u, "is_platform_admin", False)
                               or getattr(u, "parent_user_id", None) is None)
    )
    if not is_admin_user(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a tenant admin or platform admin may set the "
                   "US-person attribute",
        )
    target = crud.get_user(db, user_id)
    if target is None or target.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if not current_user.is_platform_admin and target.tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot set export-control attributes on users in other tenants",
        )
    if _dc is not None:
        _dc.set_us_person_flag(
            db, user_id=user_id, us_person=body.us_person,
            tenant_id=target.tenant_id, actor=current_user,
        )
    db.commit()
    # CE reports the state that actually exists (no paid flag -> False), so a
    # client that PATCHes then GETs sees the truth, never a phantom flag.
    return {"user_id": user_id,
            "us_person": (bool(body.us_person) if _dc is not None else False)}


@router.patch("/{user_id}/deactivate", response_model=schemas.User)
def deactivate_user_endpoint(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Deactivates a user (soft delete). Users cannot deactivate themselves.
    The user remains in the database and Keycloak but is marked as inactive,
    and their API keys are revoked (ISO 27001 A.9.2.6).

    - Standard users: 403 (no USER:deactivate capability)
    - Tenant admins: can deactivate users within their own tenant
    - Platform admins: can deactivate users in any tenant (tenant selector)
    """
    try:
        return service.deactivate_user(db, current_user, user_id)
    except ServiceError as exc:
        _raise_http(exc)


@router.post("/{user_id}/disable", response_model=schemas.UserDisableResult)
def disable_user_endpoint(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    One-click disable (FX.112.p3b): in ONE call, soft-deactivate the user
    (status + API-key revocation), flip their Keycloak user enabled=false
    and sign them out of every active Keycloak session. Soft state only —
    no hard deletes.

    - Standard users: 403 (no USER:deactivate capability)
    - Tenant admins: can disable users within their own tenant only
      (cross-tenant target -> 404); cannot disable themselves or the
      tenant's last active admin
    - Platform admins: any tenant, but the last-admin lock still applies
    """
    try:
        return service.disable_user_one_click(db, current_user, user_id)
    except ServiceError as exc:
        _raise_http(exc)


@router.post("/{user_id}/signout-everywhere",
             response_model=schemas.UserSignOutEverywhereResult)
def sign_out_user_everywhere_endpoint(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Tenant-admin "sign out everywhere" (FX.112.p4b): revoke every session
    record of the target user and all their Keycloak sessions (best-effort).
    The target's account state is untouched — logins only.

    - Standard users: 403 (no USER:deactivate capability)
    - Tenant admins: own tenant only (cross-tenant target -> 404);
      cannot target themselves (the self-service endpoint exists)
    - Platform admins: any tenant
    """
    try:
        return service.sign_out_user_everywhere(db, current_user, user_id)
    except ServiceError as exc:
        _raise_http(exc)


@router.patch("/{user_id}/reactivate", response_model=schemas.User)
def reactivate_user_endpoint(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Reactivate a previously-deactivated user (status -> ACTIVE).

    Same authorization model as deactivate: standard users 403, tenant admins
    within their own tenant, platform admins in any tenant. Previously-revoked
    API keys stay revoked — the user mints fresh ones.
    """
    try:
        return service.reactivate_user(db, current_user, user_id)
    except ServiceError as exc:
        _raise_http(exc)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user_permanently_endpoint(
        user_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_capability(models.ResourceType.USER, "delete"))
):
    """
    Permanently deletes a user from both Keycloak and the database (hard delete).
    This action cannot be undone. Users cannot delete themselves.

    - Regular users: Can only delete users from their own tenant
    - Platform admins: Can delete users from any tenant
    """
    try:
        service.delete_user_permanently(db, current_user, user_id, keycloak_admin_client)
    except ServiceError as exc:
        _raise_http(exc)
    return


@router.get("/user_accounts", response_model=List[schemas.User])
def read_user_accounts(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Retrieve all child users associated with the current user's account.
    """
    return service.list_user_accounts(db, current_user, skip=skip, limit=limit)
