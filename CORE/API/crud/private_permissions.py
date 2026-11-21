"""Private-permissions CRUD — the tenant-unscoped lookups backing
`routers/private_permissions.py` (Gate 2b migration).

These endpoints are internal service-to-service calls (gated by
`require_service_api_key`, not a per-tenant user session), so the lookups here
are plain by-id fetches with no tenant filter — exactly matching what used to
be inline `db.query(...)` calls in the router. No policy lives here; this is
the ONLY place `db.query(models.X)` runs for this domain (services/_base.py
layering convention).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_cog(db: Session, cog_id: int) -> Optional["models.Cog"]:
    """Fetch a cog by id, excluding soft-deleted rows.

    Matches every inline
    `db.query(models.Cog).filter(models.Cog.id == ..., models.Cog.deleted_at.is_(None)).first()`
    call this router used to make directly.
    """
    return (
        db.query(models.Cog)
        .filter(models.Cog.id == cog_id, models.Cog.deleted_at.is_(None))
        .first()
    )


def get_user(db: Session, user_id: int) -> Optional["models.User"]:
    """Fetch a user by id.

    Deliberately NOT tenant-scoped: the original inline query never filtered
    by tenant either — these are cross-tenant lookups used only to enrich a
    response (email/name), not to authorize access.
    """
    return db.query(models.User).filter(models.User.id == user_id).first()


def get_tenant_permission_settings(
    db: Session, tenant_id: int
) -> Optional["models.TenantPermissionSettings"]:
    """Fetch a tenant's permission-expiry warning-schedule settings, if any."""
    return (
        db.query(models.TenantPermissionSettings)
        .filter(models.TenantPermissionSettings.tenant_id == tenant_id)
        .first()
    )
