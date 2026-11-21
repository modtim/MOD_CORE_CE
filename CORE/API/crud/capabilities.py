"""CRUD layer for the capabilities/resource-permissions domain
(openspec: stack-training-and-prewarm, Gate 2 per-domain migration).

The ONLY place `db.query(...)` lives for this domain (routers/capabilities.py).
Mirrors the original inline queries 1:1 (same filters, same eager-load choices,
same ordering) so migrating the router onto these functions is behavior
preserving. Nothing here changes tenant-scoping semantics that the original
handlers relied on for their 404-vs-403 status codes — see
services/capabilities_service.py for where the authorization checks (which
decide 403) are applied, after a tenant-scoped fetch (which decides 404),
exactly as the original router did.

NOTE: `resource_access_review` / `user_effective_access` / the tenant/ownership
lookups inside `transfer_resource_ownership` mostly delegate to
`authz_review.py` / `resource_permissions.py` (different domains, out of scope
here) — only the two `Team`/`User` lookups belonging to this router are added
below.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session, joinedload

import models
from models import ResourceType


# =============================================================================
# Capability catalog (read-only, seeded)
# =============================================================================


def list_capabilities(
    db: Session,
    *,
    resource_type: Optional[ResourceType] = None,
    category: Optional[str] = None,
    skip: int = 0,
    limit: int = 500,
) -> List[models.Capability]:
    """Bounded, filtered capability list (routers/capabilities.py:list_capabilities)."""
    query = db.query(models.Capability)
    if resource_type:
        query = query.filter(models.Capability.resource_type == resource_type)
    if category:
        query = query.filter(models.Capability.category == category)
    query = query.order_by(
        models.Capability.resource_type,
        models.Capability.display_order,
        models.Capability.name,
    )
    return query.offset(skip).limit(limit).all()


def list_all_capabilities_ordered(db: Session) -> List[models.Capability]:
    """Every capability, ordered (routers/capabilities.py:list_capabilities_grouped)."""
    return (
        db.query(models.Capability)
        .order_by(
            models.Capability.resource_type,
            models.Capability.display_order,
            models.Capability.name,
        )
        .all()
    )


def get_capability(db: Session, capability_id: int) -> Optional[models.Capability]:
    return db.query(models.Capability).filter(models.Capability.id == capability_id).first()


def get_capabilities_by_ids(db: Session, capability_ids: Iterable[int]) -> List[models.Capability]:
    return db.query(models.Capability).filter(models.Capability.id.in_(list(capability_ids))).all()


def get_capability_by_name_and_type(
    db: Session, name: str, resource_type: ResourceType
) -> Optional[models.Capability]:
    return (
        db.query(models.Capability)
        .filter(models.Capability.name == name, models.Capability.resource_type == resource_type)
        .first()
    )


# =============================================================================
# ResourcePermission grants
# =============================================================================


def list_resource_permissions(
    db: Session,
    *,
    tenant_id: int,
    is_platform_admin: bool,
    resource_type: Optional[ResourceType] = None,
    resource_id: Optional[int] = None,
    user_id: Optional[int] = None,
    team_id: Optional[int] = None,
    role_id: Optional[int] = None,
    include_expired: bool = False,
    skip: int = 0,
    limit: int = 100,
) -> List[models.ResourcePermission]:
    """routers/capabilities.py:list_resource_permissions — the bulk of the filter
    logic (tenant scoping for non-admins, optional filters, expiry window)."""
    query = db.query(models.ResourcePermission).options(
        joinedload(models.ResourcePermission.capability)
    )

    if not is_platform_admin:
        query = query.filter(models.ResourcePermission.tenant_id == tenant_id)

    if resource_type:
        query = query.filter(models.ResourcePermission.resource_type == resource_type)
    if resource_id is not None:
        query = query.filter(models.ResourcePermission.resource_id == resource_id)
    if user_id:
        query = query.filter(models.ResourcePermission.user_id == user_id)
    if team_id:
        query = query.filter(models.ResourcePermission.team_id == team_id)
    if role_id:
        query = query.filter(models.ResourcePermission.role_id == role_id)

    if not include_expired:
        now = datetime.now(timezone.utc)
        query = query.filter(
            (models.ResourcePermission.expires_at == None)  # noqa: E711
            | (models.ResourcePermission.expires_at > now)
        )

    return query.offset(skip).limit(limit).all()


def get_resource_permission(
    db: Session,
    permission_id: int,
    *,
    tenant_id: int,
    with_capability: bool = False,
) -> Optional[models.ResourcePermission]:
    """Tenant-scoped fetch by id — a legit 404 (row absent or wrong tenant), same
    as the original router. `with_capability=True` eager-loads `.capability`
    (used where the handler reads `permission.capability.name` afterwards)."""
    query = db.query(models.ResourcePermission)
    if with_capability:
        query = query.options(joinedload(models.ResourcePermission.capability))
    return query.filter(
        models.ResourcePermission.id == permission_id,
        models.ResourcePermission.tenant_id == tenant_id,
    ).first()


def find_duplicate_resource_permission(
    db: Session,
    *,
    tenant_id: int,
    capability_id: int,
    resource_type: ResourceType,
    resource_id: Optional[int],
    user_id: Optional[int],
    team_id: Optional[int],
    role_id: Optional[int],
) -> Optional[models.ResourcePermission]:
    return (
        db.query(models.ResourcePermission)
        .filter(
            models.ResourcePermission.tenant_id == tenant_id,
            models.ResourcePermission.capability_id == capability_id,
            models.ResourcePermission.resource_type == resource_type,
            models.ResourcePermission.resource_id == resource_id,
            models.ResourcePermission.user_id == user_id,
            models.ResourcePermission.team_id == team_id,
            models.ResourcePermission.role_id == role_id,
        )
        .first()
    )


def create_resource_permission(
    db: Session,
    *,
    tenant_id: int,
    capability_id: int,
    resource_type: ResourceType,
    resource_id: Optional[int],
    user_id: Optional[int],
    team_id: Optional[int],
    role_id: Optional[int],
    conditions,
    expires_at,
    granted_by_user_id: int,
    commit: bool = True,
) -> models.ResourcePermission:
    db_permission = models.ResourcePermission(
        tenant_id=tenant_id,
        capability_id=capability_id,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=user_id,
        team_id=team_id,
        role_id=role_id,
        conditions=conditions,
        expires_at=expires_at,
        granted_by_user_id=granted_by_user_id,
    )
    db.add(db_permission)
    if commit:
        db.commit()
        db.refresh(db_permission)
    return db_permission


def commit_and_refresh(db: Session, obj) -> None:
    db.commit()
    db.refresh(obj)


def update_resource_permission_fields(
    db: Session,
    permission: models.ResourcePermission,
    *,
    conditions=None,
    expires_at=None,
) -> models.ResourcePermission:
    if conditions is not None:
        permission.conditions = conditions
    if expires_at is not None:
        permission.expires_at = expires_at
    db.commit()
    db.refresh(permission)
    return permission


def revoke_resource_permission(db: Session, permission: models.ResourcePermission) -> None:
    permission.revoked_at = datetime.now(timezone.utc)
    permission.is_active = False
    db.commit()


# =============================================================================
# Role <-> Capability
# =============================================================================


def get_role(
    db: Session,
    role_id: int,
    *,
    tenant_id: int,
    with_capabilities: bool = False,
) -> Optional[models.CustomRole]:
    query = db.query(models.CustomRole)
    if with_capabilities:
        query = query.options(joinedload(models.CustomRole.capabilities))
    return query.filter(
        models.CustomRole.id == role_id,
        models.CustomRole.tenant_id == tenant_id,
        models.CustomRole.deleted_at.is_(None),
    ).first()


def replace_role_capabilities(
    db: Session, role: models.CustomRole, capabilities: Sequence[models.Capability]
) -> models.CustomRole:
    role.capabilities = list(capabilities)
    db.commit()
    db.refresh(role)
    return role


def add_capability_to_role(
    db: Session, role: models.CustomRole, capability: models.Capability
) -> bool:
    """Returns True if the capability was newly added (mirrors original
    idempotent `if capability not in role.capabilities` guard)."""
    if capability not in role.capabilities:
        role.capabilities.append(capability)
        db.commit()
        return True
    return False


def remove_capability_from_role(
    db: Session, role: models.CustomRole, capability: models.Capability
) -> bool:
    if capability in role.capabilities:
        role.capabilities.remove(capability)
        db.commit()
        return True
    return False


# =============================================================================
# Permission check / effective-permissions support reads
# =============================================================================


def find_active_direct_permission(
    db: Session,
    *,
    tenant_id: int,
    capability_id: int,
    user_id: int,
    resource_id: Optional[int],
    now: datetime,
) -> Optional[models.ResourcePermission]:
    return (
        db.query(models.ResourcePermission)
        .filter(
            models.ResourcePermission.tenant_id == tenant_id,
            models.ResourcePermission.capability_id == capability_id,
            models.ResourcePermission.user_id == user_id,
            models.ResourcePermission.is_active == True,  # noqa: E712
            (models.ResourcePermission.resource_id == resource_id)
            | (models.ResourcePermission.resource_id == None),  # noqa: E711
            (models.ResourcePermission.expires_at == None)  # noqa: E711
            | (models.ResourcePermission.expires_at > now),
        )
        .first()
    )


def get_role_with_capabilities_unscoped(
    db: Session, role_id: int
) -> Optional[models.CustomRole]:
    """NOT tenant-scoped — matches the original `check_permission` /
    `get_user_effective_permissions` lookups, which trust `current_user`'s own
    `custom_role_id`/`target_user.custom_role_id` rather than re-filtering by
    tenant. Excludes soft-deleted roles."""
    return (
        db.query(models.CustomRole)
        .options(joinedload(models.CustomRole.capabilities))
        .filter(models.CustomRole.id == role_id, models.CustomRole.deleted_at.is_(None))
        .first()
    )


def list_user_team_memberships(db: Session, user_id: int):
    return (
        db.query(models.team_members_association)
        .filter(models.team_members_association.c.user_id == user_id)
        .all()
    )


def get_active_team(db: Session, team_id: int) -> Optional[models.Team]:
    return (
        db.query(models.Team)
        .filter(models.Team.id == team_id, models.Team.deleted_at.is_(None))
        .first()
    )


def find_active_team_permission(
    db: Session,
    *,
    tenant_id: int,
    capability_id: int,
    team_id: int,
    resource_id: Optional[int],
    now: datetime,
) -> Optional[models.ResourcePermission]:
    return (
        db.query(models.ResourcePermission)
        .filter(
            models.ResourcePermission.tenant_id == tenant_id,
            models.ResourcePermission.capability_id == capability_id,
            models.ResourcePermission.team_id == team_id,
            models.ResourcePermission.is_active == True,  # noqa: E712
            (models.ResourcePermission.resource_id == resource_id)
            | (models.ResourcePermission.resource_id == None),  # noqa: E711
            (models.ResourcePermission.expires_at == None)  # noqa: E711
            | (models.ResourcePermission.expires_at > now),
        )
        .first()
    )


def get_user_in_tenant(db: Session, user_id: int, tenant_id: int) -> Optional[models.User]:
    return (
        db.query(models.User)
        .filter(models.User.id == user_id, models.User.tenant_id == tenant_id)
        .first()
    )


def list_active_direct_permissions_for_user(
    db: Session,
    *,
    user_id: int,
    now: datetime,
    resource_type: Optional[ResourceType] = None,
    resource_id: Optional[int] = None,
) -> List[models.ResourcePermission]:
    query = db.query(models.ResourcePermission).options(
        joinedload(models.ResourcePermission.capability)
    ).filter(
        models.ResourcePermission.user_id == user_id,
        models.ResourcePermission.is_active == True,  # noqa: E712
        (models.ResourcePermission.expires_at == None)  # noqa: E711
        | (models.ResourcePermission.expires_at > now),
    )
    if resource_type:
        query = query.filter(models.ResourcePermission.resource_type == resource_type)
    if resource_id is not None:
        query = query.filter(
            (models.ResourcePermission.resource_id == resource_id)
            | (models.ResourcePermission.resource_id == None)  # noqa: E711
        )
    return query.all()


def list_active_team_permissions(
    db: Session,
    *,
    team_id: int,
    now: datetime,
    resource_type: Optional[ResourceType] = None,
    resource_id: Optional[int] = None,
) -> List[models.ResourcePermission]:
    query = db.query(models.ResourcePermission).options(
        joinedload(models.ResourcePermission.capability)
    ).filter(
        models.ResourcePermission.team_id == team_id,
        models.ResourcePermission.is_active == True,  # noqa: E712
        (models.ResourcePermission.expires_at == None)  # noqa: E711
        | (models.ResourcePermission.expires_at > now),
    )
    if resource_type:
        query = query.filter(models.ResourcePermission.resource_type == resource_type)
    if resource_id is not None:
        query = query.filter(
            (models.ResourcePermission.resource_id == resource_id)
            | (models.ResourcePermission.resource_id == None)  # noqa: E711
        )
    return query.all()


# =============================================================================
# Ownership-transfer support reads (the 2 db.query calls local to this router;
# the rest of transfer_resource_ownership/resource_access_review delegates to
# authz_review.py / resource_permissions.py, out of scope here)
# =============================================================================


def get_team_in_tenant(db: Session, team_id: int, tenant_id: int) -> Optional[models.Team]:
    """No soft-delete filter — matches `transfer_resource_ownership`'s original
    query exactly (unlike `get_active_team_in_tenant` below, used by
    `create_resource_permission`, which does filter it)."""
    return (
        db.query(models.Team)
        .filter(models.Team.id == team_id, models.Team.tenant_id == tenant_id)
        .first()
    )


def get_active_team_in_tenant(db: Session, team_id: int, tenant_id: int) -> Optional[models.Team]:
    return (
        db.query(models.Team)
        .filter(
            models.Team.id == team_id,
            models.Team.tenant_id == tenant_id,
            models.Team.deleted_at.is_(None),
        )
        .first()
    )
