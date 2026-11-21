"""IAM-permissions CRUD — the tenant-aware lookups backing
`routers/resource_perms/iam_permissions.py` (Gate 2b migration).

This is the ONLY place `db.query(models.X)` runs for this sub-module (role
presets + per-resource permission grant/revoke). The row-level authorization
helpers (`check_resource_permission`, `grant_resource_permission`,
`apply_role_preset`, `revoke_all_preset_permissions`, `resource_tenant_id`) stay
in the top-level `resource_permissions.py` module — they are not owned by this
migration and are imported directly, as before.

Tenant scoping matches the exact inline queries this sub-module used to run:
`get_user_by_id` is deliberately UNSCOPED (both call sites resolve the target
user by id FIRST, then apply tenant policy in the service layer — see shared
rule #5, the 404-vs-403 trap: a tenant-scoped fetch here would collapse a
legitimate 403 into a 404).
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_user_by_id(db: Session, user_id: int) -> Optional["models.User"]:
    """Fetch a user by id, tenant-UNSCOPED.

    Matches the original inline `db.query(models.User).filter(models.User.id
    == ...).first()` call sites in `apply_preset`, `list_resource_permissions`
    (per-row enrichment) and `grant_permission`. Callers apply tenant policy
    themselves — see each service function's docstring.
    """
    return db.query(models.User).filter(models.User.id == user_id).first()


def get_tenant_by_id(db: Session, tenant_id: int) -> Optional["models.Tenant"]:
    """Fetch a tenant by id (display-name lookup)."""
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


def get_capability_by_id(db: Session, capability_id: int) -> Optional["models.Capability"]:
    """Fetch a capability by id (display-name lookup)."""
    return db.query(models.Capability).filter(models.Capability.id == capability_id).first()


def list_active_resource_permissions(
    db: Session,
    resource_type: "models.ResourceType",
    resource_id: int,
    *,
    tenant_id: Optional[int] = None,
) -> List["models.ResourcePermission"]:
    """Active (non-revoked) permission rows on one resource.

    `tenant_id` is applied ONLY when the caller isn't a platform admin,
    matching the original inline query exactly (unbounded — this endpoint has
    never taken pagination; adding it now would change the OpenAPI contract).
    """
    query = db.query(models.ResourcePermission).filter(
        models.ResourcePermission.resource_type == resource_type,
        models.ResourcePermission.resource_id == resource_id,
        models.ResourcePermission.is_active == True,  # noqa: E712
        models.ResourcePermission.revoked_at.is_(None),
    )
    if tenant_id is not None:
        query = query.filter(models.ResourcePermission.tenant_id == tenant_id)
    return query.all()
