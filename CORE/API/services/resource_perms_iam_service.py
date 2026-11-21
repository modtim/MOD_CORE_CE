"""IAM-permissions service — business logic for
`routers/resource_perms/iam_permissions.py` (Gate 2b migration).

Data access -> `crud/resource_perms_iam.py` (the ONLY place db.query lives for
this sub-module). The underlying RBAC primitives — `check_resource_permission`,
`grant_resource_permission`, `apply_role_preset`, `revoke_all_preset_permissions`,
`resource_tenant_id`, `ROLE_PRESETS` — are NOT owned by this migration and stay
imported directly from the top-level `resource_permissions.py` module, exactly
as the pre-migration router did.

404-vs-403 trap (shared rule #5): `crud.get_user_by_id` is tenant-UNSCOPED. Both
`apply_role_preset` and `grant_permission` below resolve the target user by id
FIRST, then apply tenant policy here — a tenant-scoped fetch would collapse a
legitimate 403 (caller lacks the capability, or is deliberately told "not
found" to avoid leaking cross-tenant existence) into the wrong status code.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models
import crud.resource_perms_iam as crud
from resource_permissions import (
    ROLE_PRESETS,
    apply_role_preset as apply_role_preset_primitive,
    check_resource_permission,
    grant_resource_permission,
    resource_tenant_id,
    revoke_all_preset_permissions,
)
from services._base import NotFoundError, ServiceError


class ForbiddenError(ServiceError):
    """Authenticated caller lacks authorization for the action (router -> 403).

    `services._base` has no `ForbiddenError`, so it is declared here per the
    Gate 2b migration rules (shared rule #5).
    """

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class BadRequestError(ServiceError):
    """Domain validation failure (router -> 400) — mirrors the pre-migration
    inline `HTTPException(400, ...)` call sites verbatim."""

    def __init__(self, message: str = "Bad request", *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


def apply_role_preset(
    db: Session,
    *,
    preset: str,
    user_id: int,
    current_user: "models.User",
) -> Dict[str, Any]:
    """Apply a role preset to a user (admin / role-assigner only).

    Bug #5 fix (preserved verbatim): the target user is resolved by id FIRST,
    then tenant isolation is applied, so a platform admin operating on another
    tenant's user via the user-management tenant selector doesn't get a
    spurious 404. The preset is applied to the TARGET user's tenant, and the
    assignment emits an ISO 27001 AU-2 audit row.
    """
    if preset not in ROLE_PRESETS:
        raise BadRequestError(f"Unknown role preset '{preset}'.")

    # Resolve the target by id alone so 404 (no such user) is distinct from a
    # cross-tenant reference, and so a platform admin isn't wrongly 404'd by a
    # current-tenant filter.
    target_user = crud.get_user_by_id(db, user_id)
    if not target_user:
        raise NotFoundError("User not found")

    # Tenant isolation (ISO/DoD): a non-platform-admin may only assign roles
    # to users inside their own tenant, and must hold ROLE:assign. Platform
    # admins may assign to a user in any tenant (the tenant selector).
    if not current_user.is_platform_admin:
        if target_user.tenant_id != current_user.tenant_id:
            # Don't leak cross-tenant user existence — same 404 as "no user".
            raise NotFoundError("User not found")
        if not check_resource_permission(db, current_user, models.ResourceType.ROLE, "assign"):
            raise ForbiddenError("You can't assign roles")

    target_tenant_id = target_user.tenant_id
    revoked = revoke_all_preset_permissions(db, user_id)
    granted = apply_role_preset_primitive(db, target_tenant_id, user_id, preset, current_user.id)

    # ISO 27001 AU-2: assigning a role preset is an accountable admin action.
    try:
        from audit import log_admin_action
        log_admin_action(
            db,
            action=models.AuditAction.ADMIN_USER_MODIFY,
            admin_user_id=current_user.id,
            target_tenant_id=target_tenant_id,
            target_user_id=user_id,
            resource_type="role",
            resource_id=None,
            metadata={
                "operation": "role_preset_apply",
                "preset": preset,
                "revoked": revoked,
                "granted": granted,
            },
        )
    except Exception:
        # Audit is non-fatal — never fail a successful assignment on a log error.
        pass

    return {
        "message": f"Applied '{preset}' preset to {target_user.email}",
        "revoked": revoked,
        "granted": granted,
    }


def list_resource_permissions(
    db: Session,
    *,
    resource_type: str,
    resource_id: int,
    current_user: "models.User",
) -> List[Dict[str, Any]]:
    """List all permissions on a specific resource.

    Returns response-shaped dicts (field-for-field `ResourcePermissionResponse`)
    so the router can build the pydantic model without this service importing
    the router package's schemas.
    """
    try:
        rt = models.ResourceType(resource_type)
    except ValueError:
        raise BadRequestError(f"Unknown resource type: {resource_type}")

    tenant_id = None if current_user.is_platform_admin else current_user.tenant_id
    perms = crud.list_active_resource_permissions(db, rt, resource_id, tenant_id=tenant_id)

    user_cache: Dict[int, Any] = {}
    tenant_cache: Dict[int, Any] = {}
    result: List[Dict[str, Any]] = []
    for p in perms:
        user_name = None
        user_email = None
        user_tenant_id = None
        tenant_name = None
        if p.user_id:
            if p.user_id not in user_cache:
                user_cache[p.user_id] = crud.get_user_by_id(db, p.user_id)
            u = user_cache.get(p.user_id)
            if u:
                user_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or u.username
                user_email = u.email
                user_tenant_id = u.tenant_id
                if u.tenant_id not in tenant_cache:
                    t = crud.get_tenant_by_id(db, u.tenant_id)
                    tenant_cache[u.tenant_id] = t.name if t else f"Tenant {u.tenant_id}"
                tenant_name = tenant_cache.get(u.tenant_id)

        cap = crud.get_capability_by_id(db, p.capability_id)

        result.append({
            "id": p.id,
            "user_id": p.user_id,
            "user_name": user_name,
            "user_email": user_email,
            "tenant_id": user_tenant_id,
            "tenant_name": tenant_name,
            "capability_name": cap.name if cap else "unknown",
            "capability_display": cap.display_name if cap else "Unknown",
            "resource_type": resource_type,
            "resource_id": resource_id,
            "is_active": p.is_active,
            "granted_at": p.granted_at,
        })
    return result


def grant_permission(
    db: Session,
    *,
    resource_type: str,
    capability_name: str,
    resource_id: Optional[int],
    user_id: int,
    current_user: "models.User",
) -> Dict[str, Any]:
    """Grant a capability on a resource to a user."""
    try:
        rt = models.ResourceType(resource_type)
    except ValueError:
        raise BadRequestError(f"Unknown resource type: {resource_type}")

    # The resource itself must live in the caller's tenant — otherwise a grant row
    # for a cross-tenant resource_id could be minted (audit noise; not readable due
    # to router 404s, but a grant should never name another tenant's resource).
    if resource_id is not None:
        r_tenant = resource_tenant_id(db, rt, resource_id)
        if r_tenant is not None and r_tenant != current_user.tenant_id:
            raise ForbiddenError("Cannot grant permissions on a resource in another tenant")

    if not current_user.is_platform_admin:
        can_manage = check_resource_permission(db, current_user, rt, "manage_permissions", resource_id)
        can_share = check_resource_permission(db, current_user, rt, "share", resource_id)
        if not can_manage and not can_share:
            raise ForbiddenError("You can't grant permissions on this resource")

    # Use target user's tenant for the permission record
    target_user = crud.get_user_by_id(db, user_id)
    if not target_user:
        raise NotFoundError("User not found")

    # Grants stay within the tenant — no cross-tenant admin exception (ISO/DoD).
    if target_user.tenant_id != current_user.tenant_id:
        raise ForbiddenError("Cannot grant permissions to users in other tenants")

    try:
        perm = grant_resource_permission(
            db, target_user.tenant_id, user_id, rt,
            capability_name, resource_id, current_user.id,
        )
        if perm is None:
            return {"message": "Capability not applicable for this resource type", "skipped": True}
        db.commit()
        return {"message": "Permission granted", "permission_id": perm.id}
    except ValueError as e:
        raise BadRequestError(str(e))
