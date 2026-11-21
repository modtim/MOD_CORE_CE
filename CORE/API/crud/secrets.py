"""Query layer for routers/secrets.py (Gate 2b per-file service/CRUD migration).

ONLY the `db.query(...)` calls that used to live inline in the router body.
Secret VALUE storage (encryption + Vault I/O) already has its own CRUD module
at crud/secret_cud.py, re-exported via crud/__init__.py as crud.create_secret /
crud.get_secret_by_id / crud.update_secret / crud.delete_secret /
crud.get_decrypted_secret / crud.get_decrypted_secret_by_name — that module is
NOT touched here; it is out of this migration's scope and is reused as-is
(preserves its Vault/encryption behavior byte-for-byte).

This module covers the extra ad-hoc queries the router ran directly:
the CustomRole capability lookup, the PermissionRequest dedupe check, the
platform-secret-sharing admin queries, and the Tenant lookups used by the
per-secret access checks / the custom-registry entitlement gate.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session, joinedload

import models


def get_custom_role_with_capabilities(
    db: Session, custom_role_id: int
) -> Optional["models.CustomRole"]:
    """Fetch a live (non-deleted) CustomRole with its capabilities eager-loaded."""
    return (
        db.query(models.CustomRole)
        .options(joinedload(models.CustomRole.capabilities))
        .filter(
            models.CustomRole.id == custom_role_id,
            models.CustomRole.deleted_at.is_(None),
        )
        .first()
    )


def find_pending_permission_request(
    db: Session, *, requester_id: int, resource_type: str, resource_id: int
) -> Optional["models.PermissionRequest"]:
    """The existing-request dedupe check for POST /{secret_id}/request-access."""
    return (
        db.query(models.PermissionRequest)
        .filter(
            models.PermissionRequest.requester_id == requester_id,
            models.PermissionRequest.resource_type == resource_type,
            models.PermissionRequest.resource_id == resource_id,
            models.PermissionRequest.status == models.PermissionRequestStatus.PENDING,
        )
        .first()
    )


def get_tenant_by_id(db: Session, tenant_id: int) -> Optional["models.Tenant"]:
    """Used by the by-name custom-registry entitlement gate."""
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


def get_platform_secret_by_id(db: Session, secret_id: int) -> Optional["models.Secret"]:
    """Tenant-UNSCOPED fetch — platform-admin share/unshare acts across
    tenants; the caller enforces `tenant_id is None` (a real platform secret)
    itself, so scoping here would collapse a legitimate 400 into a 404."""
    return (
        db.query(models.Secret)
        .filter(
            models.Secret.id == secret_id,
            models.Secret.deleted_at.is_(None),
        )
        .first()
    )


def list_shared_platform_secrets(db: Session) -> List["models.Secret"]:
    """Platform secrets (tenant_id IS NULL) that are shared with any tenant."""
    return (
        db.query(models.Secret)
        .filter(
            models.Secret.tenant_id.is_(None),
            models.Secret.deleted_at.is_(None),
            (models.Secret.shared_with_all_tenants == True)
            | (models.Secret.shared_with_tenant_ids.isnot(None)),
        )
        .all()
    )
