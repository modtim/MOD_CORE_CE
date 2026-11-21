"""Bulk-permissions service — business/policy logic for
`routers/bulk_permissions.py` (Gate 2b migration).

Owns the grant/revoke/extend/update-capabilities orchestration the router
used to run inline, plus the transaction boundary (each bulk operation
commits once, atomically, at the end — matching pre-migration behavior
exactly). All data access goes through `crud/bulk_permissions.py` (tenant +
soft-delete scoped lookups) or the existing Gate 30 sharing-grant read model
(`cog_grants.py` / `resource_permissions.py`) — no bare `db.query(...)` lives
here.

Security note (404-vs-403 trap): every grant lookup here goes through
`get_cog_grant_by_id`, which is tenant-UNSCOPED. Tenant ownership is checked
explicitly in this service (`perm.tenant_id != current_user.tenant_id`) and
collapsed to a "not found" / not-visible result — never leaked as a
different-shaped error. Preserved verbatim from the pre-migration router.

Error convention: top-level input-validation failures and the end-of-batch
commit failure raise `services._base.ServiceError` with the SAME `code` /
message the router used to raise directly via `HTTPException` — see
`routers/bulk_permissions.py`'s `_STATUS_BY_CODE` / `_raise_http` for the
mapping back to the exact original status code. Per-item results (a single
user/permission failing within a bulk batch) are NOT exceptions — they are
appended to the batch's `results` list with `status="error"`, exactly as
before.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

from sqlalchemy.orm import Session

import audit
import models
from models import PermissionLevel
from permissions import validate_permission_grant
from cog_grants import (  # Gate 30: sharing-grant read model
    list_cog_grants, get_cog_grant_by_id, revoke_cog_grant_by_id, extend_cog_grant,
    set_cog_grant_invitation_context, CogShareSpec)
from resource_permissions import sync_cog_permission_grants, revoke_stale_cog_share_caps
from services._base import ServiceError
import crud.bulk_permissions as crud

logger = logging.getLogger(__name__)


def _check_user_capability_on_cog(db: Session, user_id: int, cog_id: int, capability: str) -> bool:
    """Indirection so a test's `patch("routers.bulk_permissions.
    check_user_capability_on_cog", ...)` still takes effect after the
    logic moved here: `routers.bulk_permissions` still imports (and can have
    its module attribute patched over) the real `permissions.
    check_user_capability_on_cog`; this re-reads that name from the router
    module at call time rather than binding it once at import time."""
    from routers.bulk_permissions import check_user_capability_on_cog
    return check_user_capability_on_cog(db, user_id, cog_id, capability)


# --------------------------------------------------------------------------- #
# POST /bulk-permissions/grant
# --------------------------------------------------------------------------- #

def bulk_grant_permissions(
    db: Session,
    request: Any,  # routers.bulk_permissions.BulkGrantPermissionRequest
    http_request: Any,
    current_user: "models.User",
) -> Dict[str, Any]:
    """Grant permissions in bulk. See router docstring for the full contract."""
    if not request.cog_ids or len(request.cog_ids) == 0:
        raise ServiceError("At least one cog_id must be provided", code="bad_request")

    if (not request.user_ids or len(request.user_ids) == 0) and (not request.emails or len(request.emails) == 0):
        raise ServiceError("At least one user_id or email must be provided", code="bad_request")

    try:
        perm_level = PermissionLevel(request.permission_level.lower())
    except ValueError:
        raise ServiceError(f"Invalid permission level: {request.permission_level}", code="bad_request")

    results = []
    successful = 0
    failed = 0

    # Check that user has can_share on all target folders
    for cog_id in request.cog_ids:
        if not _check_user_capability_on_cog(db, current_user.id, cog_id, "can_share"):
            results.append({
                "cog_id": cog_id,
                "status": "error",
                "error_message": "You don't have permission to share this folder",
            })
            failed += len(request.user_ids or []) + len(request.emails or [])
            continue

        # Prevent privilege escalation: users cannot grant permissions higher than their own
        is_valid, error_msg = validate_permission_grant(db, current_user.id, cog_id, perm_level)
        if not is_valid:
            results.append({
                "cog_id": cog_id,
                "status": "error",
                "error_message": error_msg,
            })
            failed += len(request.user_ids or []) + len(request.emails or [])
            continue

        # Grant to existing users
        if request.user_ids:
            for user_id in request.user_ids:
                try:
                    # Validate target user belongs to same tenant (cross-tenant security)
                    target_user = crud.get_user_in_tenant(db, user_id, current_user.tenant_id)
                    if not target_user:
                        results.append({
                            "cog_id": cog_id,
                            "user_id": user_id,
                            "status": "error",
                            "error_message": "Cannot grant permission to user from a different organization",
                        })
                        failed += 1
                        continue

                    # Gate 30: existing sharing grant? (reconstructed from RP)
                    _ex = list_cog_grants(db, cog_ids=[cog_id], user_id=user_id)
                    existing = _ex[0] if _ex else None

                    if existing:
                        results.append({
                            "cog_id": cog_id,
                            "user_id": user_id,
                            "status": "already_exists",
                            "permission_id": existing.id,
                        })
                        successful += 1
                        continue

                    # Create new permission
                    expires_at = None
                    if request.expires_in_days:
                        expires_at = datetime.now(timezone.utc) + timedelta(days=request.expires_in_days)

                    # Gate 30: write the grant to ResourcePermission via a TRANSIENT
                    # Permission (never persisted) driving the capability expansion.
                    # No commit — the bulk op flushes/commits once for atomicity.
                    _transient = CogShareSpec(
                        tenant_id=current_user.tenant_id,
                        target_id=cog_id,
                        user_id=user_id,
                        level=perm_level,
                        can_upload=request.can_upload,
                        can_create_subfolders=request.can_create_subfolders,
                        can_move_files=request.can_move_files,
                        can_process=request.can_process,
                        can_share=request.can_share,
                        can_delete=request.can_delete,
                        granted_by_user_id=current_user.id,
                        expires_at=expires_at,
                        invitation_context={
                            "bulk_grant": True,
                            "invitation_message": request.invitation_message,
                        },
                    )
                    sync_cog_permission_grants(db, _transient)
                    db.flush()  # assign RP ids without committing (atomic bulk op)
                    _g = list_cog_grants(db, cog_ids=[cog_id], user_id=user_id)
                    new_perm = _g[0] if _g else None

                    # Log the permission grant
                    audit.log_permission_granted(
                        db=db,
                        actor_user_id=current_user.id,
                        tenant_id=current_user.tenant_id,
                        target_user_id=user_id,
                        resource_type="cog",
                        resource_id=cog_id,
                        permission_id=(new_perm.id if new_perm else None),
                        permission_level=request.permission_level,
                        metadata={
                            "bulk_operation": True,
                            "expires_in_days": request.expires_in_days,
                        },
                        request=http_request,
                    )

                    results.append({
                        "cog_id": cog_id,
                        "user_id": user_id,
                        "status": "granted",
                        "permission_id": (new_perm.id if new_perm else None),
                    })
                    successful += 1

                except Exception as e:
                    db.rollback()
                    results.append({
                        "cog_id": cog_id,
                        "user_id": user_id,
                        "status": "error",
                        "error_message": str(e),
                    })
                    failed += 1

        # Grant to external users by email
        if request.emails:
            for email in request.emails:
                try:
                    # Check if user already exists
                    existing_user = crud.get_user_by_email_in_tenant(db, email, current_user.tenant_id)

                    if existing_user:
                        # Gate 30: existing sharing grant? (reconstructed from RP)
                        _exp = list_cog_grants(db, cog_ids=[cog_id], user_id=existing_user.id)
                        existing_perm = _exp[0] if _exp else None

                        if existing_perm:
                            results.append({
                                "cog_id": cog_id,
                                "email": email,
                                "user_id": existing_user.id,
                                "status": "already_exists",
                                "permission_id": existing_perm.id,
                            })
                            successful += 1
                            continue

                    # TODO: Create invitation for external user
                    # This would involve creating an Invitation record and triggering email
                    # For now, we'll return an error for external users
                    results.append({
                        "cog_id": cog_id,
                        "email": email,
                        "status": "error",
                        "error_message": "External user invitations not yet implemented in bulk operation",
                    })
                    failed += 1

                except Exception as e:
                    results.append({
                        "cog_id": cog_id,
                        "email": email,
                        "status": "error",
                        "error_message": str(e),
                    })
                    failed += 1

    # Commit all changes atomically at the end
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Failed to commit bulk operation: %s", e)
        raise ServiceError("Failed to commit bulk operation", code="internal_error")

    return {
        "total_requested": len(results),
        "successful": successful,
        "failed": failed,
        "results": results,
    }


# --------------------------------------------------------------------------- #
# POST /bulk-permissions/revoke
# --------------------------------------------------------------------------- #

def bulk_revoke_permissions(
    db: Session,
    request: Any,  # routers.bulk_permissions.BulkRevokeRequest
    http_request: Any,
    current_user: "models.User",
) -> Dict[str, Any]:
    """Revoke multiple permissions at once. See router docstring for contract."""
    results = []
    successful = 0
    failed = 0

    for permission_id in request.permission_ids:
        try:
            # Tenant-scoped permission lookup (exclude soft-deleted).
            # `get_cog_grant_by_id` is deliberately tenant-UNSCOPED; the
            # tenant check below is the policy layer (404-vs-403 trap: a
            # cross-tenant id collapses to "not_found", never a different
            # error shape that would leak existence).
            perm = get_cog_grant_by_id(db, permission_id)  # Gate 30: grant by read-model id
            if perm is not None and perm.tenant_id != current_user.tenant_id:
                perm = None

            if not perm:
                results.append({
                    "permission_id": permission_id,
                    "status": "not_found",
                })
                failed += 1
                continue

            # Check if user has permission to revoke
            # User can revoke if they:
            # 1. Have can_share on the target folder
            # 2. Are the person who granted it
            # 3. Own the folder
            # Exclude soft-deleted cogs
            cog = crud.get_active_cog(db, perm.target_id)  # noqa: F841 (see crud docstring)

            can_revoke = False
            # Owner access resolves through the derived-owner rule (DO-1): the
            # owner holds can_share, so the can_share check below authorizes them.
            # The former `cog.owner_id == user` branch was redundant with that.
            if perm.granted_by_user_id == current_user.id:
                can_revoke = True
            elif _check_user_capability_on_cog(db, current_user.id, perm.target_id, "can_share"):
                can_revoke = True

            if not can_revoke:
                results.append({
                    "permission_id": permission_id,
                    "status": "error",
                    "error_message": "You don't have permission to revoke this",
                })
                failed += 1
                continue

            # Log the permission revocation before deleting
            audit.log_permission_revoked(
                db=db,
                actor_user_id=current_user.id,
                tenant_id=current_user.tenant_id,
                target_user_id=perm.user_id,
                resource_type=perm.target_type.value,
                resource_id=perm.target_id,
                permission_id=perm.id,
                permission_level=perm.level.value,
                metadata={
                    "bulk_operation": True,
                },
                request=http_request,
            )

            # Gate 30: revoke every row of the grant (soft delete via revoked_at,
            # audit trail preserved). commit=False — the bulk op commits once.
            revoke_cog_grant_by_id(db, permission_id,
                                   revoked_by_user_id=current_user.id, commit=False)

            results.append({
                "permission_id": permission_id,
                "status": "revoked",
            })
            successful += 1

        except Exception as e:
            results.append({
                "permission_id": permission_id,
                "status": "error",
                "error_message": str(e),
            })
            failed += 1

    # Commit all changes atomically at the end
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Failed to commit bulk revoke: %s", e)
        raise ServiceError("Failed to commit bulk revoke", code="internal_error")

    return {
        "total_requested": len(request.permission_ids),
        "successful": successful,
        "failed": failed,
        "results": results,
    }


# --------------------------------------------------------------------------- #
# POST /bulk-permissions/extend-expiration
# --------------------------------------------------------------------------- #

def bulk_extend_expiration(
    db: Session,
    request: Any,  # routers.bulk_permissions.BulkExtendExpirationRequest
    current_user: "models.User",
) -> Dict[str, Any]:
    """Extend expiration dates for multiple permissions. See router docstring."""
    if request.extend_by_days <= 0:
        raise ServiceError("extend_by_days must be positive", code="bad_request")

    results = []
    successful = 0
    failed = 0

    for permission_id in request.permission_ids:
        try:
            # Tenant-scoped permission lookup (exclude soft-deleted)
            perm = get_cog_grant_by_id(db, permission_id)  # Gate 30: grant by read-model id
            if perm is not None and perm.tenant_id != current_user.tenant_id:
                perm = None

            if not perm:
                results.append({
                    "permission_id": permission_id,
                    "status": "error",
                    "error_message": "Permission not found",
                })
                failed += 1
                continue

            # Check if user has permission to extend (exclude soft-deleted cogs)
            cog = crud.get_active_cog(db, perm.target_id)  # noqa: F841 (see crud docstring)

            can_extend = False
            # Owner access resolves through the derived-owner rule (DO-1): owner
            # holds can_share, authorized by the can_share check below.
            if perm.granted_by_user_id == current_user.id:
                can_extend = True
            elif _check_user_capability_on_cog(db, current_user.id, perm.target_id, "can_share"):
                can_extend = True

            if not can_extend:
                results.append({
                    "permission_id": permission_id,
                    "status": "error",
                    "error_message": "You don't have permission to extend this",
                })
                failed += 1
                continue

            # Check if permission has expiration
            if not perm.expires_at:
                results.append({
                    "permission_id": permission_id,
                    "status": "no_expiration",
                    "error_message": "This permission has no expiration date",
                })
                failed += 1
                continue

            # Extend expiration
            old_expires_at = perm.expires_at
            new_expires_at = old_expires_at + timedelta(days=request.extend_by_days)
            perm.expires_at = new_expires_at

            # Update invitation context to track extension
            invitation_context = perm.invitation_context or {}
            extensions = invitation_context.get("extensions", [])
            extensions.append({
                "extended_by_user_id": current_user.id,
                "extended_at": datetime.now(timezone.utc).isoformat(),
                "days_added": request.extend_by_days,
                "old_expires_at": old_expires_at.isoformat(),
                "new_expires_at": new_expires_at.isoformat(),
            })
            invitation_context["extensions"] = extensions
            # Gate 30: persist the new expiry + tracking onto the grant's rows.
            # commit=False — the bulk op commits once.
            extend_cog_grant(db, permission_id, new_expires_at, commit=False)
            set_cog_grant_invitation_context(db, permission_id, invitation_context,
                                             commit=False)

            results.append({
                "permission_id": permission_id,
                "old_expires_at": old_expires_at.isoformat(),
                "new_expires_at": new_expires_at.isoformat(),
                "status": "extended",
            })
            successful += 1

        except Exception as e:
            results.append({
                "permission_id": permission_id,
                "status": "error",
                "error_message": str(e),
            })
            failed += 1

    # Commit all changes atomically at the end
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Failed to commit bulk extend: %s", e)
        raise ServiceError("Failed to commit bulk extend", code="internal_error")

    return {
        "total_requested": len(request.permission_ids),
        "successful": successful,
        "failed": failed,
        "results": results,
    }


# --------------------------------------------------------------------------- #
# POST /bulk-permissions/update-capabilities
# --------------------------------------------------------------------------- #

def bulk_update_capabilities(
    db: Session,
    request: Any,  # routers.bulk_permissions.BulkUpdateCapabilitiesRequest
    http_request: Any,
    current_user: "models.User",
) -> Dict[str, Any]:
    """Update capability flags for multiple permissions. See router docstring."""
    # Validate that at least one capability is being updated
    capabilities_to_update = {
        k: v for k, v in {
            "can_upload": request.can_upload,
            "can_create_subfolders": request.can_create_subfolders,
            "can_move_files": request.can_move_files,
            "can_process": request.can_process,
            "can_share": request.can_share,
            "can_delete": request.can_delete,
        }.items() if v is not None
    }

    if not capabilities_to_update:
        raise ServiceError("At least one capability must be specified", code="bad_request")

    results = []
    successful = 0
    failed = 0

    for permission_id in request.permission_ids:
        try:
            # Tenant-scoped permission lookup (exclude soft-deleted)
            perm = get_cog_grant_by_id(db, permission_id)  # Gate 30: grant by read-model id
            if perm is not None and perm.tenant_id != current_user.tenant_id:
                perm = None

            if not perm:
                results.append({
                    "permission_id": permission_id,
                    "status": "error",
                    "error_message": "Permission not found",
                })
                failed += 1
                continue

            # Check if user has permission to update (exclude soft-deleted cogs)
            cog = crud.get_active_cog(db, perm.target_id)  # noqa: F841 (see crud docstring)

            can_update = False
            # Owner access resolves through the derived-owner rule (DO-1): owner
            # holds can_share, authorized by the can_share check below.
            if perm.granted_by_user_id == current_user.id:
                can_update = True
            elif _check_user_capability_on_cog(db, current_user.id, perm.target_id, "can_share"):
                can_update = True

            if not can_update:
                results.append({
                    "permission_id": permission_id,
                    "status": "error",
                    "error_message": "You don't have permission to update this",
                })
                failed += 1
                continue

            # Gate 30: re-write the grant with the updated capability overrides
            # (same level/grantee). sync adds caps, revoke_stale removes the ones
            # no longer granted. commit=False — the bulk op commits once.
            _t = CogShareSpec(
                tenant_id=perm.tenant_id, target_id=perm.target_id,
                user_id=perm.user_id, team_id=perm.team_id,
                keycloak_group_id=perm.keycloak_group_id,
                level=perm.level, granted_by_user_id=perm.granted_by_user_id,
                expires_at=perm.expires_at, is_external_user=perm.is_external_user,
                invitation_context=perm.invitation_context,
                can_upload=capabilities_to_update.get("can_upload", perm.can_upload),
                can_create_subfolders=capabilities_to_update.get(
                    "can_create_subfolders", perm.can_create_subfolders),
                can_move_files=capabilities_to_update.get("can_move_files", perm.can_move_files),
                can_process=capabilities_to_update.get("can_process", perm.can_process),
                can_share=capabilities_to_update.get("can_share", perm.can_share),
                can_delete=capabilities_to_update.get("can_delete", perm.can_delete),
            )
            sync_cog_permission_grants(db, _t)
            revoke_stale_cog_share_caps(db, _t)

            # Log the permission update (flush to get ID)
            audit.log_permission_updated(
                db=db,
                actor_user_id=current_user.id,
                tenant_id=current_user.tenant_id,
                target_user_id=perm.user_id,
                resource_type=perm.target_type.value,
                resource_id=perm.target_id,
                permission_id=perm.id,
                permission_level=perm.level.value,
                metadata={
                    "bulk_operation": True,
                    "capabilities_updated": capabilities_to_update,
                },
                request=http_request,
            )

            results.append({
                "permission_id": permission_id,
                "status": "updated",
            })
            successful += 1

        except Exception as e:
            results.append({
                "permission_id": permission_id,
                "status": "error",
                "error_message": str(e),
            })
            failed += 1

    # Commit all changes atomically at the end
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Failed to commit bulk update: %s", e)
        raise ServiceError("Failed to commit bulk update", code="internal_error")

    return {
        "total_requested": len(request.permission_ids),
        "successful": successful,
        "failed": failed,
        "results": results,
    }
