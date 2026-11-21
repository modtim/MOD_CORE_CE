"""RBAC domain — service layer (Community Edition).

Community Edition ships the built-in system roles (Admin, Standard User,
Guest, External Collaborator) and lets an admin assign them to users. Custom
role creation, editing and deletion, and viewing another tenant's roles, are
not part of Community Edition.

SECURITY-CRITICAL — the 404-vs-403 trap:
`assign_role` fetches the target user by id with NO tenant filter
(`crud.rbac.get_user_by_id`), then applies tenant policy HERE: a
non-platform-admin whose target is in another tenant gets the exact same
404 ("User not found") as a genuinely nonexistent user — never a 403 —
so cross-tenant user existence is never leaked. Do not "fix" this to a
403 — that would BE the leak this code exists to prevent.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

import crud
import models
import schemas
from crud import rbac as rbac_crud
from services._base import NotFoundError, ServiceError


class ForbiddenError(ServiceError):
    """A real 403 — the caller is authenticated but not allowed to do this.

    Distinct from `NotFoundError`, which the assignment 404-trap uses
    deliberately even where the underlying cause is a tenant boundary, so
    that code path must never raise this class instead.
    """

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


def emit_role_audit(
    db: Session,
    current_user: "models.User",
    *,
    resource_id: int,
    operation: str,
    role_name: str,
    action: Optional["models.AuditAction"] = None,
    target_user_id: Optional[int] = None,
    target_tenant_id: Optional[int] = None,
) -> None:
    """Emit an append-only audit row for a role event (ISO 27001 AU-2). No-op
    when the actor has no id (keeps the endpoint resilient and avoids a
    failed-transaction insert). Non-fatal.

    ``target_tenant_id`` records the tenant the action AFFECTED — pass the
    target user's tenant for a cross-tenant platform-admin assignment so the
    audit trail attributes it to the right org, not the admin's own.
    """
    actor_id = getattr(current_user, "id", None)
    if actor_id is None:
        return
    from audit import log_admin_action

    log_admin_action(
        db,
        action=action or models.AuditAction.ADMIN_CONFIG_CHANGE,
        admin_user_id=actor_id,
        target_tenant_id=target_tenant_id if target_tenant_id is not None
                         else getattr(current_user, "tenant_id", None),
        target_user_id=target_user_id,
        resource_type="role",
        resource_id=resource_id,
        metadata={"operation": operation, "role_name": role_name},
    )


def resolve_roles_target_tenant(
    db: Session, current_user: "models.User", tenant_id: Optional[int]
) -> int:
    """Determine which tenant's roles to list.

    No `tenant_id` override -> the caller's own tenant. An override naming the
    caller's own tenant is the same listing. Naming any other tenant is a real
    403: Community Edition does not offer cross-tenant role views.
    """
    if tenant_id is not None and tenant_id != current_user.tenant_id:
        if not current_user.is_platform_admin:
            raise ForbiddenError(
                "Only platform administrators can view roles from other tenants."
            )
        raise ForbiddenError(
            "Viewing the roles of other tenants is not available in Community Edition."
        )
    return current_user.tenant_id


def list_roles(
    db: Session, current_user: "models.User", *, tenant_id: Optional[int], skip: int, limit: int
):
    """List the caller's tenant's built-in system roles (Admin / Standard
    User / Guest / External Collaborator).

    The system-role filter is applied BEFORE paging, otherwise a tenant whose
    other roles sort first would lose its system roles on the first page.
    `limit=None` = no SQL LIMIT (the tenant's own role rows only — the tenant
    filter is still inside `crud.get_custom_roles`), then slice here.
    """
    target_tenant_id = resolve_roles_target_tenant(db, current_user, tenant_id)
    rows = crud.get_custom_roles(db, tenant_id=target_tenant_id, skip=0, limit=None)
    system_rows = [r for r in rows if getattr(r, "is_system_role", False)]
    return system_rows[skip: skip + limit] if limit else system_rows[skip:]


def assign_role(
    db: Session, current_user: "models.User", *, user_id: int, role_id: int
) -> models.User:
    """Assign a role to a user.

    A platform admin may assign a role to a user in ANY tenant; a
    non-platform-admin may only assign within their own tenant. The target
    user is resolved by id FIRST, tenant-UNSCOPED (`crud.rbac.get_user_by_id`)
    so a platform admin isn't wrongly 404'd by a current-tenant filter, and
    so "no such user" is distinguishable from "cross-tenant reference" at
    THIS layer even though both surface as the same 404 to the caller
    (never leak cross-tenant user existence — see module docstring).
    The role is looked up in the TARGET user's tenant (roles are
    tenant-scoped), and the assignment is audited against that tenant.
    """
    target_user = rbac_crud.get_user_by_id(db, user_id)
    if not target_user:
        raise NotFoundError("User not found")

    if not current_user.is_platform_admin:
        if target_user.tenant_id != current_user.tenant_id:
            # Don't leak cross-tenant user existence — same 404 as "no user".
            raise NotFoundError("User not found")

    target_tenant_id = target_user.tenant_id

    # The role must live in the TARGET user's tenant (roles are tenant-scoped).
    db_role = crud.get_custom_role(db, role_id=role_id, tenant_id=target_tenant_id)
    if not db_role:
        raise NotFoundError("Role not found in the target tenant")

    user_update = schemas.UserUpdate(custom_role_id=role_id)
    updated_user = crud.update_user(db=db, db_user=target_user, user_update=user_update)
    emit_role_audit(
        db, current_user, resource_id=role_id, operation="role_assign",
        role_name=db_role.name, action=models.AuditAction.ADMIN_USER_MODIFY,
        target_user_id=user_id, target_tenant_id=target_tenant_id,
    )
    return updated_user
