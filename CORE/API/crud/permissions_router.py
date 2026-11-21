"""Permissions-router CRUD — the tenant-aware lookups backing
`routers/permissions.py` (Gate 2b migration).

This is the ONLY place `db.query(models.X)` runs for this domain (the
sharing-management endpoints for cogs/workflows). Named `permissions_router`
(not `permissions`) to avoid clashing with the top-level `permissions.py`
policy module (`can_user_share_cog`, `create_permission`, etc.), which stays
where it is and is imported directly by the router/service as before.

Tenant scoping matches the exact inline queries this router used to run:
some lookups are deliberately UNSCOPED (read-only enrichment, or a
cross-tenant admin-audit check that needs to see the real tenant to detect the
mismatch) — see each function's docstring for which.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

import models


def get_user_by_id(db: Session, user_id: int) -> Optional["models.User"]:
    """Fetch a user by id, tenant-UNSCOPED.

    Matches the original inline
    `db.query(models.User).filter(models.User.id == perm.user_id).first()`
    used only to enrich a permission row with email/name/tenant for display.
    """
    return db.query(models.User).filter(models.User.id == user_id).first()


def get_tenant_by_id(db: Session, tenant_id: int) -> Optional["models.Tenant"]:
    """Fetch a tenant by id (display-name lookup)."""
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


def get_cog_by_id(db: Session, cog_id: int) -> Optional["models.Cog"]:
    """Fetch a cog by id, tenant-UNSCOPED.

    Used only by the cross-tenant admin-audit check (a platform admin was
    denied `can_user_share_cog` — this looks the cog up regardless of tenant
    to tell whether that denial was in fact a cross-tenant access attempt).
    """
    return db.query(models.Cog).filter(models.Cog.id == cog_id).first()


def get_user_in_tenant(db: Session, user_id: int, tenant_id: int) -> Optional["models.User"]:
    """Fetch a user by id, scoped to `tenant_id` (cross-tenant grant guard)."""
    return (
        db.query(models.User)
        .filter(models.User.id == user_id, models.User.tenant_id == tenant_id)
        .first()
    )


def get_team_in_tenant(db: Session, team_id: int, tenant_id: int) -> Optional["models.Team"]:
    """Fetch a team by id, scoped to `tenant_id` (cross-tenant grant guard)."""
    return (
        db.query(models.Team)
        .filter(models.Team.id == team_id, models.Team.tenant_id == tenant_id)
        .first()
    )


def get_cog_in_tenant(db: Session, cog_id: int, tenant_id: int) -> Optional["models.Cog"]:
    """Fetch a cog by id, scoped to `tenant_id`, excluding soft-deleted rows."""
    return (
        db.query(models.Cog)
        .filter(
            models.Cog.id == cog_id,
            models.Cog.tenant_id == tenant_id,
            models.Cog.deleted_at.is_(None),
        )
        .first()
    )


def get_workflow_in_tenant(
    db: Session, workflow_id: int, tenant_id: int
) -> Optional["models.Workflow"]:
    """Fetch a workflow by id, scoped to `tenant_id`, excluding soft-deleted rows."""
    return (
        db.query(models.Workflow)
        .filter(
            models.Workflow.id == workflow_id,
            models.Workflow.tenant_id == tenant_id,
            models.Workflow.deleted_at.is_(None),
        )
        .first()
    )


def get_active_workflow_resource_permissions(
    db: Session, user_id: int, workflow_id: int
) -> List[Tuple["models.ResourcePermission", "models.Capability"]]:
    """Every active, non-revoked `ResourcePermission` + its `Capability` this
    user holds on this workflow.

    Used twice by the workflow-share flow: once to find capabilities that fell
    out of a new share level (revoke them) and once — AFTER that revoke is
    flushed — to find the capabilities that stayed (stamp an optional expiry
    on them). Calling this again after the first mutation intentionally
    re-queries post-autoflush, matching the original inline behavior of two
    separate `db.query(...)` calls in the same request.
    """
    return (
        db.query(models.ResourcePermission, models.Capability)
        .join(
            models.Capability,
            models.ResourcePermission.capability_id == models.Capability.id,
        )
        .filter(
            models.ResourcePermission.user_id == user_id,
            models.ResourcePermission.resource_type == models.ResourceType.WORKFLOW,
            models.ResourcePermission.resource_id == workflow_id,
            models.ResourcePermission.is_active == True,  # noqa: E712
            models.ResourcePermission.revoked_at.is_(None),
            models.Capability.resource_type == models.ResourceType.WORKFLOW,
        )
        .all()
    )
