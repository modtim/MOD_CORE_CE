"""Permission Requests — CRUD layer (Gate 2b query-layer migration).

The ONLY place `db.query(...)` lives for this domain. Pure data access: no
policy, no authorization decisions, no HTTP concerns. `services.permission_requests_service`
is the caller and owns every policy decision built on top of these reads
(including the tenant/ownership 404-vs-403 split — see that module's docstring).

`get_request_by_id` is intentionally tenant-UNSCOPED: the service layer applies
tenant policy explicitly so a legitimate 403 (wrong owner, right tenant) never
collapses into a 404 that an over-scoped query would have produced.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

import models
from models import PermissionRequestStatus


def get_request_by_id(db: Session, request_id: int) -> Optional["models.PermissionRequest"]:
    """Fetch one permission request by id. Tenant-UNSCOPED (see module docstring)."""
    return db.query(models.PermissionRequest).filter(
        models.PermissionRequest.id == request_id
    ).first()


def get_pending_request_for_cog(
    db: Session, requester_id: int, cog_id: int
) -> Optional["models.PermissionRequest"]:
    """A requester's still-pending request against a given cog, if any (dup check)."""
    return db.query(models.PermissionRequest).filter(
        models.PermissionRequest.requester_id == requester_id,
        models.PermissionRequest.cog_id == cog_id,
        models.PermissionRequest.status == PermissionRequestStatus.PENDING,
    ).first()


def list_requests_for_requester(
    db: Session, requester_id: int, status_enum: Optional[PermissionRequestStatus] = None
) -> List["models.PermissionRequest"]:
    """All requests a user created, newest first, optionally filtered by status.

    NOTE: unbounded (matches the pre-migration endpoint exactly — the endpoint
    has no `limit`/`offset` params, and adding pagination here would change the
    response contract). Flagged for the enforce gate, not fixed here.
    """
    query = db.query(models.PermissionRequest).filter(
        models.PermissionRequest.requester_id == requester_id
    )
    if status_enum is not None:
        query = query.filter(models.PermissionRequest.status == status_enum)
    return query.order_by(models.PermissionRequest.created_at.desc()).all()


def list_owned_cog_ids(db: Session, owner_id: int, tenant_id: int) -> List[int]:
    return [r[0] for r in db.query(models.Cog.id).filter(
        models.Cog.owner_id == owner_id,
        models.Cog.tenant_id == tenant_id,
    ).all()]


def list_owned_workflow_ids(db: Session, created_by_user_id: int, tenant_id: int) -> List[int]:
    return [r[0] for r in db.query(models.Workflow.id).filter(
        models.Workflow.created_by_user_id == created_by_user_id,
        models.Workflow.tenant_id == tenant_id,
    ).all()]


def list_owned_secret_ids(db: Session, created_by_user_id: int, tenant_id: int) -> List[int]:
    return [r[0] for r in db.query(models.Secret.id).filter(
        models.Secret.created_by_user_id == created_by_user_id,
        models.Secret.tenant_id == tenant_id,
    ).all()]


def list_pending_requests(
    db: Session,
    tenant_id: int,
    *,
    is_platform_admin: bool,
    owned_cog_ids: Sequence[int],
    owned_wf_ids: Sequence[int],
    owned_secret_ids: Sequence[int],
) -> List["models.PermissionRequest"]:
    """Pending requests visible to a reviewer: platform admins see every pending
    request in their tenant; everyone else only requests against resources they
    own (cog/workflow/secret)."""
    base = db.query(models.PermissionRequest).filter(
        models.PermissionRequest.status == PermissionRequestStatus.PENDING,
        models.PermissionRequest.tenant_id == tenant_id,
    )
    if is_platform_admin:
        return base.order_by(models.PermissionRequest.created_at.asc()).all()
    return base.filter(or_(
        and_(models.PermissionRequest.resource_type == "cog",
             models.PermissionRequest.cog_id.in_(owned_cog_ids or [-1])),
        and_(models.PermissionRequest.resource_type == "workflow",
             models.PermissionRequest.resource_id.in_(owned_wf_ids or [-1])),
        and_(models.PermissionRequest.resource_type == "secret",
             models.PermissionRequest.resource_id.in_(owned_secret_ids or [-1])),
    )).order_by(models.PermissionRequest.created_at.asc()).all()


def get_cog_for_tenant(db: Session, cog_id: int, tenant_id: int) -> Optional["models.Cog"]:
    """Cog lookup scoped to tenant only — matches the original create-request
    existence check exactly (intentionally NO `deleted_at` filter here; that is
    the pre-existing behavior, not a fix)."""
    return db.query(models.Cog).filter(
        models.Cog.id == cog_id,
        models.Cog.tenant_id == tenant_id,
    ).first()


def get_active_cog(db: Session, cog_id: Optional[int]) -> Optional["models.Cog"]:
    if cog_id is None:
        return None
    return db.query(models.Cog).filter(
        models.Cog.id == cog_id,
        models.Cog.deleted_at.is_(None),
    ).first()


def get_active_workflow(db: Session, workflow_id: Optional[int]) -> Optional["models.Workflow"]:
    if workflow_id is None:
        return None
    return db.query(models.Workflow).filter(
        models.Workflow.id == workflow_id,
        models.Workflow.deleted_at.is_(None),
    ).first()


def get_active_secret(db: Session, secret_id: Optional[int]) -> Optional["models.Secret"]:
    if secret_id is None:
        return None
    return db.query(models.Secret).filter(
        models.Secret.id == secret_id,
        models.Secret.deleted_at.is_(None),
    ).first()


def get_user(db: Session, user_id: Optional[int]) -> Optional["models.User"]:
    if user_id is None:
        return None
    return db.query(models.User).filter(models.User.id == user_id).first()
