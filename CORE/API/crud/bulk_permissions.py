"""Bulk-permissions CRUD — the tenant + soft-delete-scoped lookups backing
`routers/bulk_permissions.py` (Gate 2b query-layer migration).

This is the ONLY place `db.query(models.X)` runs for this domain
(services/_base.py layering convention). All grant/revoke/extend/update
POLICY lives in `services/bulk_permissions_service.py`; the higher-level
sharing-grant read-model calls this domain already used (`cog_grants.py` /
`resource_permissions.py` — `list_cog_grants`, `get_cog_grant_by_id`,
`revoke_cog_grant_by_id`, `extend_cog_grant`, `sync_cog_permission_grants`,
etc.) are NOT `db.query` and are out of scope for this migration; the service
still calls them directly, same as the pre-migration router did.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_user_in_tenant(db: Session, user_id: int, tenant_id: int) -> Optional["models.User"]:
    """Fetch a user by id, scoped to `tenant_id`.

    Used to guard against granting a permission to a user from a different
    organization (cross-tenant security check in the bulk-grant flow).
    """
    return (
        db.query(models.User)
        .filter(models.User.id == user_id, models.User.tenant_id == tenant_id)
        .first()
    )


def get_user_by_email_in_tenant(db: Session, email: str, tenant_id: int) -> Optional["models.User"]:
    """Fetch a user by email, scoped to `tenant_id` (bulk-grant-by-email flow)."""
    return (
        db.query(models.User)
        .filter(models.User.email == email, models.User.tenant_id == tenant_id)
        .first()
    )


def get_active_cog(db: Session, cog_id: int) -> Optional["models.Cog"]:
    """Fetch a cog by id, excluding soft-deleted rows.

    NOTE (flagged, not fixed — out of scope for a mechanical migration): the
    result of this lookup is currently UNUSED by the bulk revoke / extend /
    update-capabilities handlers. A comment at each call site explains the
    former `cog.owner_id == user` ownership branch was removed in favor of
    the derived-owner rule (DO-1), which the accompanying `can_share` check
    already covers — but the now-pointless `cog = ...` fetch itself was left
    in place. This migration preserves that exact (harmless but dead) extra
    query rather than silently dropping it.
    """
    return (
        db.query(models.Cog)
        .filter(models.Cog.id == cog_id, models.Cog.deleted_at.is_(None))
        .first()
    )
