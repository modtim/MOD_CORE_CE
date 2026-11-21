"""Service layer for the capabilities/resource-permissions domain
(openspec: stack-training-and-prewarm, Gate 2 per-domain migration of
routers/capabilities.py).

Owns policy/orchestration for: the capability catalog, ResourcePermission
grants, role<->capability management, permission checks/effective-permissions,
and the access-review/ownership-transfer surface. All data access goes through
`crud/capabilities.py`; the only db.query left here (none) or in the router
(none) is inside crud.

NOTE on the two colliding Pydantic model names elsewhere in this domain
(`GrantRequest`, `UserPermissionSummary` — defined in
`routers/platform_capabilities.py` / `routers/permission_analytics.py` /
`routers/resource_perms/__init__.py`, NOT in this file): they are untouched by
this migration — this module doesn't reference them.

Error convention: methods raise `ServiceError` (`services._base`) or the local
`ForbiddenError` below; `routers/capabilities.py` maps them back to the EXACT
original `HTTPException` status codes/messages (see `_raise_http` there).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

import models
import schemas
from models import ResourceType
from services._base import NotFoundError, ServiceError
import crud.capabilities as capabilities_crud


class ForbiddenError(ServiceError):
    """Caller lacks the privilege for the requested action (router maps -> 403).

    `services/_base.py` (CRUDBase/ServiceError/NotFoundError/ConflictError)
    deliberately has no 403 — SHARED RULES for this migration wave call for a
    domain-local one where a handler needs it (the 404-vs-403 trap: a
    tenant-scoped CRUD fetch legitimately 404s; a privilege check on a row the
    caller CAN see legitimately 403s — this domain has several of those).
    """

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


# =============================================================================
# Capability catalog
# =============================================================================


def list_capabilities(
    db: Session,
    *,
    resource_type: Optional[ResourceType],
    category: Optional[str],
    skip: int,
    limit: int,
) -> List[models.Capability]:
    return capabilities_crud.list_capabilities(
        db, resource_type=resource_type, category=category, skip=skip, limit=limit
    )


def list_capabilities_grouped(db: Session) -> List[Dict[str, Any]]:
    """Group the full catalog by resource_type -> category -> [CapabilityResponse].
    Ported verbatim from the original handler body."""
    capabilities = capabilities_crud.list_all_capabilities_ordered(db)

    grouped: Dict[Any, Dict[str, Any]] = {}
    for cap in capabilities:
        rt = cap.resource_type
        if rt not in grouped:
            grouped[rt] = {
                "resource_type": rt,
                "resource_type_display": rt.value.replace("_", " ").title(),
                "categories": {},
            }

        category = cap.category or "General"
        if category not in grouped[rt]["categories"]:
            grouped[rt]["categories"][category] = []

        grouped[rt]["categories"][category].append(schemas.CapabilityResponse.model_validate(cap))

    return list(grouped.values())


def get_capability(db: Session, capability_id: int) -> models.Capability:
    capability = capabilities_crud.get_capability(db, capability_id)
    if not capability:
        raise NotFoundError("Capability not found")
    return capability


# =============================================================================
# ResourcePermission grants
# =============================================================================

_MAX_LIST_ROWS = 5000


def list_resource_permissions(
    db: Session,
    *,
    current_user: models.User,
    resource_type: Optional[ResourceType],
    resource_id: Optional[int],
    user_id: Optional[int],
    team_id: Optional[int],
    role_id: Optional[int],
    include_expired: bool,
    skip: int,
    limit: Optional[int],
) -> List[models.ResourcePermission]:
    if limit is None:
        effective_limit = _MAX_LIST_ROWS if resource_id is not None else 100
    else:
        effective_limit = min(max(limit, 0), _MAX_LIST_ROWS)

    return capabilities_crud.list_resource_permissions(
        db,
        tenant_id=current_user.tenant_id,
        is_platform_admin=current_user.is_platform_admin,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=user_id,
        team_id=team_id,
        role_id=role_id,
        include_expired=include_expired,
        skip=skip,
        limit=effective_limit,
    )


def create_resource_permission(
    db: Session,
    permission: "schemas.ResourcePermissionCreate",
    current_user: models.User,
) -> Tuple[models.ResourcePermission, models.Capability]:
    capability = capabilities_crud.get_capability(db, permission.capability_id)
    if not capability:
        raise NotFoundError("Capability not found")

    targets = [permission.user_id, permission.team_id, permission.role_id]
    if sum(1 for t in targets if t is not None) != 1:
        raise ServiceError(
            "Exactly one of user_id, team_id, or role_id must be specified",
            code="bad_request",
        )

    # AUTHORIZATION: granting a capability on a resource is privileged — only a
    # principal that can manage the resource's grants (owner via derived
    # ownership, or a holder of manage_permissions/share) may do it.
    from resource_permissions import can_manage_resource_grant

    if not can_manage_resource_grant(
        db, current_user, permission.resource_type, permission.resource_id
    ):
        raise ForbiddenError("You don't have permission to grant access on this resource")

    if permission.user_id:
        target = capabilities_crud.get_user_in_tenant(db, permission.user_id, current_user.tenant_id)
        if not target:
            raise NotFoundError("User not found in this tenant")

    if permission.team_id:
        target = capabilities_crud.get_active_team_in_tenant(
            db, permission.team_id, current_user.tenant_id
        )
        if not target:
            raise NotFoundError("Team not found in this tenant")

    if permission.role_id:
        target = capabilities_crud.get_role(db, permission.role_id, tenant_id=current_user.tenant_id)
        if not target:
            raise NotFoundError("Role not found in this tenant")

    existing = capabilities_crud.find_duplicate_resource_permission(
        db,
        tenant_id=current_user.tenant_id,
        capability_id=permission.capability_id,
        resource_type=permission.resource_type,
        resource_id=permission.resource_id,
        user_id=permission.user_id,
        team_id=permission.team_id,
        role_id=permission.role_id,
    )
    if existing:
        raise ServiceError("This permission already exists", code="bad_request")

    db_permission = capabilities_crud.create_resource_permission(
        db,
        tenant_id=current_user.tenant_id,
        capability_id=permission.capability_id,
        resource_type=permission.resource_type,
        resource_id=permission.resource_id,
        user_id=permission.user_id,
        team_id=permission.team_id,
        role_id=permission.role_id,
        conditions=permission.conditions,
        expires_at=permission.expires_at,
        granted_by_user_id=current_user.id,
    )
    return db_permission, capability


def create_resource_permissions_bulk(
    db: Session,
    bulk_request: "schemas.ResourcePermissionBulkCreate",
    current_user: models.User,
) -> List[Tuple[models.ResourcePermission, models.Capability]]:
    targets = [bulk_request.user_id, bulk_request.team_id, bulk_request.role_id]
    if sum(1 for t in targets if t is not None) != 1:
        raise ServiceError(
            "Exactly one of user_id, team_id, or role_id must be specified",
            code="bad_request",
        )

    from resource_permissions import can_manage_resource_grant

    if not can_manage_resource_grant(
        db, current_user, bulk_request.resource_type, bulk_request.resource_id
    ):
        raise ForbiddenError("You don't have permission to grant access on this resource")

    capabilities = capabilities_crud.get_capabilities_by_ids(db, bulk_request.capability_ids)
    if len(capabilities) != len(bulk_request.capability_ids):
        found_ids = {c.id for c in capabilities}
        missing = set(bulk_request.capability_ids) - found_ids
        raise NotFoundError(f"Capabilities not found: {missing}")

    cap_map = {c.id: c for c in capabilities}
    created: List[models.ResourcePermission] = []

    for capability_id in bulk_request.capability_ids:
        existing = capabilities_crud.find_duplicate_resource_permission(
            db,
            tenant_id=current_user.tenant_id,
            capability_id=capability_id,
            resource_type=bulk_request.resource_type,
            resource_id=bulk_request.resource_id,
            user_id=bulk_request.user_id,
            team_id=bulk_request.team_id,
            role_id=bulk_request.role_id,
        )
        if existing:
            continue

        db_permission = capabilities_crud.create_resource_permission(
            db,
            tenant_id=current_user.tenant_id,
            capability_id=capability_id,
            resource_type=bulk_request.resource_type,
            resource_id=bulk_request.resource_id,
            user_id=bulk_request.user_id,
            team_id=bulk_request.team_id,
            role_id=bulk_request.role_id,
            conditions=bulk_request.conditions,
            expires_at=bulk_request.expires_at,
            granted_by_user_id=current_user.id,
            commit=False,
        )
        created.append(db_permission)

    db.commit()

    result: List[Tuple[models.ResourcePermission, models.Capability]] = []
    for perm in created:
        db.refresh(perm)
        result.append((perm, cap_map[perm.capability_id]))
    return result


def update_resource_permission(
    db: Session,
    permission_id: int,
    update: "schemas.ResourcePermissionUpdate",
    current_user: models.User,
) -> models.ResourcePermission:
    permission = capabilities_crud.get_resource_permission(
        db, permission_id, tenant_id=current_user.tenant_id, with_capability=True
    )
    if not permission:
        raise NotFoundError("Permission not found")

    # AUTHORIZATION: editing a grant is reserved to a resource manager (owner /
    # manage_permissions / share holder).
    from resource_permissions import can_manage_resource_grant

    if not can_manage_resource_grant(db, current_user, permission.resource_type, permission.resource_id):
        raise ForbiddenError("You don't have permission to modify this grant")

    return capabilities_crud.update_resource_permission_fields(
        db, permission, conditions=update.conditions, expires_at=update.expires_at
    )


def delete_resource_permission(db: Session, permission_id: int, current_user: models.User) -> None:
    permission = capabilities_crud.get_resource_permission(
        db, permission_id, tenant_id=current_user.tenant_id, with_capability=False
    )
    if not permission:
        raise NotFoundError("Permission not found")

    from resource_permissions import can_manage_resource_grant

    if not can_manage_resource_grant(db, current_user, permission.resource_type, permission.resource_id):
        raise ForbiddenError("You don't have permission to revoke this grant")

    capabilities_crud.revoke_resource_permission(db, permission)


# =============================================================================
# Role <-> Capability
# =============================================================================


def get_role_capabilities(db: Session, role_id: int, current_user: models.User) -> List[models.Capability]:
    role = capabilities_crud.get_role(db, role_id, tenant_id=current_user.tenant_id, with_capabilities=True)
    if not role:
        raise NotFoundError("Role not found")
    return role.capabilities



# =============================================================================
# Permission check / effective permissions
# =============================================================================


def check_permission(
    db: Session, request: "schemas.PermissionCheckRequest", current_user: models.User
) -> "schemas.PermissionCheckResponse":
    capability = capabilities_crud.get_capability_by_name_and_type(
        db, request.capability_name, request.resource_type
    )
    if not capability:
        return schemas.PermissionCheckResponse(
            allowed=False,
            capability_name=request.capability_name,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            source=None,
            conditions=None,
        )

    now = datetime.now(timezone.utc)

    direct_perm = capabilities_crud.find_active_direct_permission(
        db,
        tenant_id=current_user.tenant_id,
        capability_id=capability.id,
        user_id=current_user.id,
        resource_id=request.resource_id,
        now=now,
    )
    if direct_perm:
        return schemas.PermissionCheckResponse(
            allowed=True,
            capability_name=request.capability_name,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            source="direct",
            conditions=direct_perm.conditions,
        )

    if current_user.custom_role_id:
        role = capabilities_crud.get_role_with_capabilities_unscoped(db, current_user.custom_role_id)
        if role and capability in role.capabilities:
            return schemas.PermissionCheckResponse(
                allowed=True,
                capability_name=request.capability_name,
                resource_type=request.resource_type,
                resource_id=request.resource_id,
                source=f"role:{role.name}",
                conditions=None,
            )

    user_teams = capabilities_crud.list_user_team_memberships(db, current_user.id)
    for team_membership in user_teams:
        team_perm = capabilities_crud.find_active_team_permission(
            db,
            tenant_id=current_user.tenant_id,
            capability_id=capability.id,
            team_id=team_membership.team_id,
            resource_id=request.resource_id,
            now=now,
        )
        if team_perm:
            team = capabilities_crud.get_active_team(db, team_membership.team_id)
            return schemas.PermissionCheckResponse(
                allowed=True,
                capability_name=request.capability_name,
                resource_type=request.resource_type,
                resource_id=request.resource_id,
                source=f"team:{team.name if team else 'Unknown'}",
                conditions=team_perm.conditions,
            )

    return schemas.PermissionCheckResponse(
        allowed=False,
        capability_name=request.capability_name,
        resource_type=request.resource_type,
        resource_id=request.resource_id,
        source=None,
        conditions=None,
    )


def get_user_effective_permissions(
    db: Session,
    user_id: int,
    resource_type: Optional[ResourceType],
    resource_id: Optional[int],
    current_user: models.User,
) -> List["schemas.EffectivePermissions"]:
    target_user = capabilities_crud.get_user_in_tenant(db, user_id, current_user.tenant_id)
    if not target_user:
        raise NotFoundError("User not found")

    now = datetime.now(timezone.utc)
    permissions_map: Dict[Any, Dict[str, Any]] = defaultdict(lambda: {"capabilities": set(), "sources": {}})

    for perm in capabilities_crud.list_active_direct_permissions_for_user(
        db, user_id=user_id, now=now, resource_type=resource_type, resource_id=resource_id
    ):
        key = (perm.resource_type, perm.resource_id)
        cap_name = perm.capability.name
        permissions_map[key]["capabilities"].add(cap_name)
        permissions_map[key]["sources"][cap_name] = "direct"

    if target_user.custom_role_id:
        role = capabilities_crud.get_role_with_capabilities_unscoped(db, target_user.custom_role_id)
        if role:
            for cap in role.capabilities:
                if resource_type and cap.resource_type != resource_type:
                    continue
                key = (cap.resource_type, None)
                permissions_map[key]["capabilities"].add(cap.name)
                if cap.name not in permissions_map[key]["sources"]:
                    permissions_map[key]["sources"][cap.name] = f"role:{role.name}"

    user_teams = capabilities_crud.list_user_team_memberships(db, user_id)
    for team_membership in user_teams:
        team = capabilities_crud.get_active_team(db, team_membership.team_id)
        if not team:
            continue
        team_name = team.name

        for perm in capabilities_crud.list_active_team_permissions(
            db,
            team_id=team_membership.team_id,
            now=now,
            resource_type=resource_type,
            resource_id=resource_id,
        ):
            key = (perm.resource_type, perm.resource_id)
            cap_name = perm.capability.name
            permissions_map[key]["capabilities"].add(cap_name)
            if cap_name not in permissions_map[key]["sources"]:
                permissions_map[key]["sources"][cap_name] = f"team:{team_name}"

    result = []
    for (res_type, res_id), data in permissions_map.items():
        result.append(
            schemas.EffectivePermissions(
                resource_type=res_type,
                resource_id=res_id,
                capabilities=list(data["capabilities"]),
                sources=data["sources"],
            )
        )
    return result


# =============================================================================
# Access review / effective access / ownership transfer
# =============================================================================


def resource_access_review(
    db: Session,
    resource_type: ResourceType,
    resource_id: int,
    *,
    exhaustive: bool,
    as_of: Optional[str],
    current_user: models.User,
) -> Dict[str, Any]:
    if not current_user.is_platform_admin:
        raise ForbiddenError("Platform admin only")

    from authz_review import who_can_access, _resource_tenant

    rt_tenant = _resource_tenant(db, resource_type, resource_id)
    if rt_tenant is None:
        raise NotFoundError("Resource not found")
    if rt_tenant != current_user.tenant_id:
        raise ForbiddenError("Cross-tenant access review is not permitted")

    if as_of is not None:
        from authz_review import reconstruct_resource_access_as_of

        try:
            at = datetime.fromisoformat(as_of)
        except ValueError:
            raise ServiceError("as_of must be an ISO-8601 timestamp", code="unprocessable_entity")
        return {
            "resource_type": resource_type.value,
            "resource_id": resource_id,
            "reconstruction": reconstruct_resource_access_as_of(db, resource_type, resource_id, at),
        }

    return {
        "resource_type": resource_type.value,
        "resource_id": resource_id,
        "exhaustive": exhaustive,
        "principals": who_can_access(db, resource_type, resource_id, exhaustive=exhaustive),
    }


def user_effective_access(
    db: Session,
    user_id: int,
    resource_type: Optional[ResourceType],
    current_user: models.User,
) -> Dict[str, Any]:
    if not current_user.is_platform_admin and current_user.id != user_id:
        raise ForbiddenError("Platform admin or self only")

    target = capabilities_crud.get_user_in_tenant(db, user_id, current_user.tenant_id)
    if not target:
        raise NotFoundError("User not found")

    from authz_review import what_can_user_reach

    return {"user_id": user_id, "access": what_can_user_reach(db, target, resource_type)}


def transfer_resource_ownership(
    db: Session,
    resource_type: ResourceType,
    resource_id: int,
    body: Any,
    request: Any,
    current_user: models.User,
) -> Dict[str, Any]:
    import audit
    from authz_review import _resource_tenant
    from resource_permissions import _derived_owner_allows, set_resource_owner
    from models import PrincipalType

    if bool(body.new_owner_user_id) == bool(body.new_owner_team_id):
        raise ServiceError(
            "Provide exactly one of new_owner_user_id or new_owner_team_id",
            code="unprocessable_entity",
        )

    rt_tenant = _resource_tenant(db, resource_type, resource_id)
    if rt_tenant is None:
        raise NotFoundError("Resource not found")
    if rt_tenant != current_user.tenant_id:
        raise ForbiddenError("Cross-tenant transfer is not permitted")

    is_owner = _derived_owner_allows(db, current_user, resource_type, resource_id)
    if not (is_owner or current_user.is_platform_admin):
        raise ForbiddenError("Only the current owner or a platform admin may transfer ownership")

    if body.new_owner_team_id:
        team = capabilities_crud.get_team_in_tenant(db, body.new_owner_team_id, rt_tenant)
        if not team:
            raise NotFoundError("Team not found in this tenant")
        principal_type, principal_id = PrincipalType.TEAM, body.new_owner_team_id
    else:
        new_owner = capabilities_crud.get_user_in_tenant(db, body.new_owner_user_id, rt_tenant)
        if not new_owner:
            raise NotFoundError("New owner not found in this tenant")
        principal_type, principal_id = PrincipalType.USER, body.new_owner_user_id

    try:
        old_principal, new_principal = set_resource_owner(
            db, resource_type, resource_id, principal_type, principal_id, actor_user_id=current_user.id
        )
    except ValueError as e:
        raise ServiceError(str(e), code="bad_request")

    audit.log_admin_action(
        db,
        action=models.AuditAction.PERMISSION_UPDATED,
        admin_user_id=current_user.id,
        request=request,
        resource_type=resource_type.value,
        resource_id=resource_id,
        metadata={
            "event": "ownership_transfer",
            "old_owner": list(old_principal) if old_principal else None,
            "new_owner": list(new_principal),
            "old_owner_user_id": old_principal[1] if (old_principal and old_principal[0] == "user") else None,
            "new_owner_user_id": principal_id if principal_type == PrincipalType.USER else None,
        },
    )
    db.commit()
    return {
        "resource_type": resource_type.value,
        "resource_id": resource_id,
        "old_owner": list(old_principal) if old_principal else None,
        "new_owner": list(new_principal),
        "old_owner_user_id": old_principal[1] if (old_principal and old_principal[0] == "user") else None,
        "new_owner_user_id": principal_id if principal_type == PrincipalType.USER else None,
    }
