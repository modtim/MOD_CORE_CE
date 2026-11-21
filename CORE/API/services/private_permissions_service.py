"""Private-permissions service — business logic for
`routers/private_permissions.py` (Gate 2b migration).

Owns the policy/orchestration the router used to do inline: bucketing
expiring grants by tenant warning-schedule, queuing expiration-warning
digest emails, revoking expired grants, and capability checks used by
worker/service callers. All data access goes through `crud/private_permissions.py`
or the existing `cog_grants` / `permissions` read models — no bare
`db.query(...)` lives here.

Behavior is a straight move of the router's prior inline logic — including
pre-existing quirks (e.g. `send_expiration_warning`'s in-memory
`invitation_context` mutation on the duck-typed `CogGrant` object, which
`db.commit()` does not actually persist, unlike `track_notification`'s use of
`set_cog_grant_invitation_context`) — preserved byte-for-byte rather than
"fixed", per the Gate 2b snapshot-parity requirement.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from crud import private_permissions as crud
from cog_grants import (  # Gate 30: sharing-grant read model
    get_cog_grant_by_id,
    list_cog_grants,
    revoke_cog_grant_by_id,
    set_cog_grant_invitation_context,
)
from permissions import (
    check_user_capability_on_cog,
    get_effective_capability,
    get_user_permission_on_cog,
    is_permission_expired,
)
from services._base import NotFoundError, ServiceError


class PermissionNotExpiredError(ServiceError):
    """Raised by `revoke_expired_permission` when the grant hasn't expired yet
    (router maps this -> 400, matching the original inline check)."""

    def __init__(self, message: str = "Permission has not expired yet"):
        super().__init__(message, code="not_expired")


# --------------------------------------------------------------------------- #
# GET /private/permissions/expiring
# --------------------------------------------------------------------------- #

def _permission_to_dict(db: Session, perm) -> Optional[Dict[str, Any]]:  # Gate 30: CogGrant
    """Convert a grant to the response dict shape. Returns None for
    soft-deleted cogs (skip)."""
    cog = crud.get_cog(db, perm.target_id)
    if not cog:
        return None  # Skip permissions on deleted cogs

    user = crud.get_user(db, perm.user_id)
    granter = crud.get_user(db, perm.granted_by_user_id)

    return {
        "id": perm.id,
        "user_id": perm.user_id,
        "user_email": user.email if user else None,
        "user_name": f"{user.first_name} {user.last_name}" if user else None,
        "cog_id": perm.target_id,
        "cog_name": cog.name,
        "level": perm.level.value,
        "expires_at": perm.expires_at.isoformat() if perm.expires_at else None,
        "granted_by_user_id": perm.granted_by_user_id,
        "granted_by_email": granter.email if granter else None,
        "granted_by_name": f"{granter.first_name} {granter.last_name}" if granter else None,
        "is_external_user": perm.is_external_user,
        "tenant_id": user.tenant_id if user else None,
    }


def list_expiring_permissions(db: Session) -> Dict[str, List[Dict[str, Any]]]:
    """Bucket permissions expiring soon (or already expired) by tenant-specific
    warning schedule. Returns the four `ExpiringPermissionsResponse` lists."""
    now = datetime.now(timezone.utc)

    # Gate 30: all sharing grants that carry an expiry (reconstructed from
    # ResourcePermission). Bucketing below is unchanged.
    all_expiring = [g for g in list_cog_grants(db) if g.expires_at is not None]

    expiring_7d: List[Dict[str, Any]] = []
    expiring_3d: List[Dict[str, Any]] = []
    expiring_1d: List[Dict[str, Any]] = []
    expired: List[Dict[str, Any]] = []

    # Cache tenant settings to avoid repeated queries
    tenant_settings_cache: Dict[int, List[int]] = {}

    for perm in all_expiring:
        if not perm.expires_at:
            continue

        # Check if already expired
        if is_permission_expired(perm):
            perm_dict = _permission_to_dict(db, perm)
            if perm_dict:  # Skip soft-deleted cogs
                expired.append(perm_dict)
            continue

        # Get user's tenant to determine warning schedule
        user = crud.get_user(db, perm.user_id)
        if not user:
            continue

        tenant_id = user.tenant_id

        # Get tenant settings (with caching)
        if tenant_id not in tenant_settings_cache:
            tenant_settings = crud.get_tenant_permission_settings(db, tenant_id)

            if tenant_settings and tenant_settings.warning_schedule:
                schedule = tenant_settings.warning_schedule
            else:
                schedule = [7, 3, 1]  # Default schedule

            tenant_settings_cache[tenant_id] = schedule

        schedule = tenant_settings_cache[tenant_id]

        # Calculate time until expiration
        time_until_expiration = perm.expires_at - now
        days_until_expiration = time_until_expiration.total_seconds() / 86400  # Convert to days

        metadata = perm.invitation_context or {}
        warnings_sent = metadata.get("warnings_sent", [])

        # Check against tenant-specific schedule
        # Use the three most distant warning points from the schedule
        if len(schedule) >= 3:
            warning_days = sorted(schedule, reverse=True)[:3]  # Get 3 largest values
        else:
            warning_days = [7, 3, 1]  # Fallback

        # Convert permission to dict once, skip if cog is soft-deleted
        perm_dict = _permission_to_dict(db, perm)
        if not perm_dict:
            continue  # Skip permissions on soft-deleted cogs

        # First warning (usually 7 days)
        first_warning_days = warning_days[0]
        if (first_warning_days - 0.5) <= days_until_expiration <= (first_warning_days + 0.5):
            warning_key = f"{first_warning_days}d"
            if warning_key not in warnings_sent:
                expiring_7d.append(perm_dict)

        # Second warning (usually 3 days)
        second_warning_days = warning_days[1] if len(warning_days) > 1 else 3
        if (second_warning_days - 0.5) <= days_until_expiration <= (second_warning_days + 0.5):
            warning_key = f"{second_warning_days}d"
            if warning_key not in warnings_sent:
                expiring_3d.append(perm_dict)

        # Third warning (usually 1 day)
        third_warning_days = warning_days[2] if len(warning_days) > 2 else 1
        if (third_warning_days - 0.5) <= days_until_expiration <= (third_warning_days + 0.5):
            warning_key = f"{third_warning_days}d"
            if warning_key not in warnings_sent:
                expiring_1d.append(perm_dict)

    return {
        "expiring_7d": expiring_7d,
        "expiring_3d": expiring_3d,
        "expiring_1d": expiring_1d,
        "expired": expired,
    }


# --------------------------------------------------------------------------- #
# POST /private/permissions/{permission_id}/send-expiration-warning
# --------------------------------------------------------------------------- #

def send_expiration_warning(
    db: Session,
    permission_id: int,
    *,
    days_until_expiration: int,
    warning_type: str,
) -> Dict[str, Any]:
    """Queue expiration-warning digest emails for a grant's user + granter."""
    # Get the permission (exclude soft-deleted permissions)
    perm = get_cog_grant_by_id(db, permission_id)
    if not perm:
        raise NotFoundError("Permission not found")

    # Get related entities (exclude soft-deleted cogs)
    user = crud.get_user(db, perm.user_id)
    cog = crud.get_cog(db, perm.target_id)
    granter = crud.get_user(db, perm.granted_by_user_id)

    if not user or not cog:
        raise NotFoundError("User or cog not found")

    # Update permission metadata to track that we sent this warning
    invitation_context = perm.invitation_context or {}
    warnings_sent = invitation_context.get("warnings_sent", [])

    if warning_type not in warnings_sent:
        warnings_sent.append(warning_type)
        invitation_context["warnings_sent"] = warnings_sent
        invitation_context[f"warning_{warning_type}_sent_at"] = datetime.now(timezone.utc).isoformat()

        perm.invitation_context = invitation_context
        db.commit()

    # Prepare email data
    email_data = {
        "permission_id": perm.id,
        "user_email": user.email,
        "user_name": f"{user.first_name} {user.last_name}",
        "cog_name": cog.name,
        "permission_level": perm.level.value,
        "expires_at": perm.expires_at.isoformat() if perm.expires_at else None,
        "days_until_expiration": days_until_expiration,
        "granted_by_name": f"{granter.first_name} {granter.last_name}" if granter else "Unknown",
        "granted_by_email": granter.email if granter else None,
        "is_external_user": perm.is_external_user,
    }

    # Send expiration warning emails
    try:
        # Prepare capabilities dict
        capabilities = {
            "can_upload": get_effective_capability(perm, "can_upload"),
            "can_create_subfolders": get_effective_capability(perm, "can_create_subfolders"),
            "can_move_files": get_effective_capability(perm, "can_move_files"),
            "can_process": get_effective_capability(perm, "can_process"),
            "can_share": get_effective_capability(perm, "can_share"),
            "can_delete": get_effective_capability(perm, "can_delete"),
        }

        # Send warning to the user
        user_email_conf = {
            "email_type": "user_warning",
            "user_name": email_data["user_name"],
            "user_email": email_data["user_email"],
            "folder_name": email_data["cog_name"],
            "permission_level": email_data["permission_level"],
            "expiration_date": email_data["expires_at"],
            "days_until_expiration": days_until_expiration,
            "granted_by": email_data["granted_by_name"],
            "granted_by_email": email_data["granted_by_email"],
            "is_external_user": email_data["is_external_user"],
            "capabilities": capabilities,
        }

        # Queue the warning for the daily digest instead of sending
        # immediately. Expiration warnings are reminders, not actions —
        # a one-day delay before the user sees them is fine, and
        # batching kills the "one email per permission" spam operators
        # were getting. queue_digest_event INSERTs into
        # pending_permission_emails; services.permission_digest drains
        # rows at 09:00 user-local. See utils/permission_digest.py.
        from utils.permission_digest import queue_digest_event

        queue_digest_event(
            db=db,
            user_id=user.id,
            tenant_id=perm.tenant_id,
            event_type="permission_expiration",
            payload={**user_email_conf, "tenant_id": perm.tenant_id},
        )

        # Same for the granter notice — they don't need real-time
        # delivery either, and granters often grant many permissions
        # so they're the most spammed by per-event sends.
        if granter:
            granter_email_conf = {
                "email_type": "granter_notice",
                "granter_name": email_data["granted_by_name"],
                "granter_email": email_data["granted_by_email"],
                "user_name": email_data["user_name"],
                "user_email": email_data["user_email"],
                "folder_name": email_data["cog_name"],
                "permission_level": email_data["permission_level"],
                "expiration_date": email_data["expires_at"],
                "granted_date": perm.granted_at.strftime("%Y-%m-%d") if perm.granted_at else "Unknown",
                "days_until_expiration": days_until_expiration,
                "invitation_message": invitation_context.get("invitation_message"),
            }

            queue_digest_event(
                db=db,
                user_id=granter.id,
                tenant_id=perm.tenant_id,
                event_type="permission_expiration",
                payload=granter_email_conf,
            )

        logging.info(f"Triggered expiration warning emails for permission {perm.id}")

    except Exception as e:
        logging.error(f"Failed to trigger email DAG: {e}")
        # Don't fail the request if email trigger fails
        pass

    return {
        "status": "warning_sent",
        "permission_id": permission_id,
        "warning_type": warning_type,
        "recipient_email": user.email,
    }


# --------------------------------------------------------------------------- #
# POST /private/permissions/{permission_id}/revoke-expired
# --------------------------------------------------------------------------- #

def revoke_expired_permission(db: Session, permission_id: int) -> None:
    """Revoke (soft-delete) a permission that has already expired."""
    # Get the permission (exclude already soft-deleted permissions)
    perm = get_cog_grant_by_id(db, permission_id)
    if not perm:
        raise NotFoundError("Permission not found")

    # Verify it's actually expired
    if not is_permission_expired(perm):
        raise PermissionNotExpiredError("Permission has not expired yet")

    # Log the revocation (exclude soft-deleted cogs for info purposes)
    user = crud.get_user(db, perm.user_id)
    cog = crud.get_cog(db, perm.target_id)

    logging.info(
        f"Auto-revoking expired permission {perm.id}: "
        f"user={user.email if user else 'unknown'}, "
        f"cog={cog.name if cog else 'unknown'}, "
        f"expired_at={perm.expires_at}"
    )

    # Gate 30: revoke every row of the grant (soft delete via revoked_at, so the
    # ResourcePermission audit trail is preserved for ISO 27001).
    revoke_cog_grant_by_id(db, permission_id)


# --------------------------------------------------------------------------- #
# POST /private/permissions/check-capability
# --------------------------------------------------------------------------- #

def check_user_capability(
    db: Session, *, user_id: int, cog_id: int, capability: str
) -> Dict[str, Any]:
    """Check whether a user has a given capability on a cog, plus owner/level."""
    # Check if user is the folder owner (exclude soft-deleted cogs)
    cog = crud.get_cog(db, cog_id)
    is_owner = bool(cog and cog.owner_id == user_id)

    # Check capability
    has_permission = check_user_capability_on_cog(db, user_id, cog_id, capability)

    # Get permission level if exists
    permission = get_user_permission_on_cog(db, user_id, cog_id)
    permission_level = permission.level.value if permission else None

    return {
        "user_id": user_id,
        "cog_id": cog_id,
        "capability": capability,
        "has_permission": has_permission,
        "permission_level": permission_level,
        "is_owner": is_owner,
    }


# --------------------------------------------------------------------------- #
# POST /private/permissions/{permission_id}/track-notification
# --------------------------------------------------------------------------- #

def track_notification(
    db: Session, permission_id: int, tracking_data: Dict[str, Any]
) -> Dict[str, Any]:
    """Record which notification channels successfully sent for a warning."""
    # Get the permission (exclude soft-deleted permissions)
    perm = get_cog_grant_by_id(db, permission_id)
    if not perm:
        raise NotFoundError("Permission not found")

    # Update invitation_context with notification tracking
    invitation_context = perm.invitation_context or {}

    # Track warnings sent
    warnings_sent = invitation_context.get("warnings_sent", [])
    warning_type = tracking_data.get("warning_type")

    if warning_type and warning_type not in warnings_sent:
        warnings_sent.append(warning_type)
        invitation_context["warnings_sent"] = warnings_sent

    # Track notification channels
    notification_tracking = invitation_context.get("notification_tracking", {})
    if warning_type not in notification_tracking:
        notification_tracking[warning_type] = {}

    notification_tracking[warning_type] = {
        "channels": tracking_data.get("channels", {}),
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }

    invitation_context["notification_tracking"] = notification_tracking
    # Gate 30: persist the updated tracking onto the grant's ResourcePermission rows.
    set_cog_grant_invitation_context(db, permission_id, invitation_context)

    logging.info(
        f"Tracked notification for permission {permission_id}: "
        f"warning={warning_type}, channels={tracking_data.get('channels')}"
    )

    return {
        "status": "tracked",
        "permission_id": permission_id,
        "warning_type": warning_type,
        "channels": tracking_data.get("channels"),
    }
