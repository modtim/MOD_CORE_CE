"""Permission Analytics CRUD — the ONLY place `db.query(...)` lives for the
`routers/permission_analytics.py` domain.

The sharing-grant rows themselves come from `cog_grants.list_cog_grants`
(Gate 30's reconstructed read model over `ResourcePermission`) — that is
already a query-layer module and is called directly from
`services/permission_analytics_service.py`, matching the existing precedent
in `services/permission_templates_service.py` / `services/permissions_router_service.py`.

This module holds only the plain lookups the router used to run inline:
Cog / owning-User lookups (soft-delete-aware), PermissionTemplate listing,
and the two User lookups (single-by-id and external-users list), all
tenant-scoped where the router originally scoped them.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_active_cog(db: Session, cog_id: int) -> Optional["models.Cog"]:
    """A single non-soft-deleted cog by id (no tenant filter — callers already
    derive `cog_id` from tenant-scoped grants, matching prior inline queries)."""
    return db.query(models.Cog).filter(
        models.Cog.id == cog_id,
        models.Cog.deleted_at.is_(None),
    ).first()


def get_user_by_id(db: Session, user_id: int) -> Optional["models.User"]:
    """A single user by id, unscoped (used to resolve a cog's owner display
    name — matches the prior inline query exactly)."""
    return db.query(models.User).filter(models.User.id == user_id).first()


def list_permission_templates_for_tenant(db: Session, tenant_id: int) -> List["models.PermissionTemplate"]:
    return db.query(models.PermissionTemplate).filter(
        models.PermissionTemplate.tenant_id == tenant_id,
    ).all()


def get_tenant_user_by_id(db: Session, user_id: int, tenant_id: int) -> Optional["models.User"]:
    """A single user by id, tenant-scoped. Returns `None` for a cross-tenant
    id (the router maps that to 404 — preserved as-is; this endpoint never
    distinguished 403 from 404 for another tenant's user)."""
    return db.query(models.User).filter(
        models.User.id == user_id,
        models.User.tenant_id == tenant_id,
    ).first()


def list_external_users_for_tenant(db: Session, tenant_id: int) -> List["models.User"]:
    return db.query(models.User).filter(
        models.User.tenant_id == tenant_id,
        models.User.is_external == True,  # noqa: E712
    ).all()
