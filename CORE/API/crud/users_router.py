"""users-router CRUD — the tenant/admin lookups backing `routers/users.py`
(Gate 2b migration).

This is the ONLY place `db.query(models.Tenant)` / the *admin* (cross-tenant,
include-deleted) `db.query(models.User)` lookups run for this domain. Named
`users_router` (not `users`) to avoid clashing with the existing
`crud/user_cud.py` module (re-exported as top-level `crud.get_user_in_tenant`,
`crud.get_users_in_tenant`, etc.) — those functions already cover the
tenant-scoped, non-deleted, ordinary-user paths and are REUSED as-is (imported
via the `crud` package, not duplicated here).

Everything here is a query site that `crud/user_cud.py` / `crud/tenant_cud.py`
did NOT already cover: unbounded platform-admin tenant listing, an
include-deleted tenant/user fetch (admin can act on a soft-deleted or
half-deleted row), a plain tenant-by-name existence check, and the cascade-
delete user listing (deliberately NOT filtered by
`keycloak_user_id.isnot(None)` the way `get_users_in_tenant` is, since a
tenant delete must sweep every live user row, not just ones with a linked
Keycloak account).
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def list_tenants(db: Session) -> List["models.Tenant"]:
    """All non-deleted tenants (Platform Admin `GET /tenants`)."""
    return (
        db.query(models.Tenant)
        .filter(models.Tenant.deleted_at.is_(None))
        .all()
    )


def get_tenant_by_name(db: Session, name: str) -> Optional["models.Tenant"]:
    """Fetch a tenant by exact name (uniqueness check on tenant create)."""
    return db.query(models.Tenant).filter(models.Tenant.name == name).first()


def get_tenant_include_deleted(db: Session, tenant_id: int) -> Optional["models.Tenant"]:
    """Fetch a tenant by id, INCLUDING soft-deleted rows (delete is idempotent
    — the caller must be able to see an already-deleted tenant to no-op)."""
    return (
        db.query(models.Tenant)
        .filter(models.Tenant.id == tenant_id)
        .execution_options(include_deleted=True)
        .first()
    )


def get_tenant_users_for_cascade_delete(db: Session, tenant_id: int) -> List["models.User"]:
    """Every live (non-deleted) user in a tenant, for tenant-delete cascade.

    Deliberately UNFILTERED on `keycloak_user_id` (unlike
    `crud.get_users_in_tenant`) — a tenant delete must scrub every user row
    regardless of whether it ever got a linked Keycloak account.
    """
    return (
        db.query(models.User)
        .filter(models.User.tenant_id == tenant_id)
        .filter(models.User.deleted_at.is_(None))
        .all()
    )


def get_user_include_deleted(db: Session, user_id: int) -> Optional["models.User"]:
    """Fetch a user by id, tenant-UNSCOPED, INCLUDING soft-deleted rows.

    Platform-admin god-mode lookup (deactivate/reactivate any user in any
    tenant, including one mid-way through a half-deleted state).
    """
    return (
        db.query(models.User)
        .filter(models.User.id == user_id)
        .execution_options(include_deleted=True)
        .first()
    )


def get_user_in_tenant_include_deleted(
    db: Session, user_id: int, tenant_id: int
) -> Optional["models.User"]:
    """Fetch a user by id scoped to `tenant_id`, INCLUDING soft-deleted rows.

    Used by the hard-delete endpoint's platform-admin branch, which — per the
    ISO/DoD own-tenant-only isolation rule for user deletion — is still scoped
    to the ACTING admin's own tenant (not god-mode; see
    `services.users_router_service.delete_user_permanently`).
    """
    return (
        db.query(models.User)
        .filter(models.User.id == user_id, models.User.tenant_id == tenant_id)
        .execution_options(include_deleted=True)
        .first()
    )
