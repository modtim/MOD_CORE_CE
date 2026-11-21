"""User CRUD operations - User management within tenants."""
from typing import Optional, List
import os
import logging

from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone

import models, schemas
from audit import SYSTEM_USER_USERNAME

# Setup logging
logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data)
from services.debug_mode import is_debug_mode


def exclude_system_users(query):
    """Filter out the non-login `mod-system` audit actor (tenant_1, id 155)
    from any `db.query(models.User...)` — it must never appear in a user
    list or be counted toward a tenant's active-user/seat total.
    Reusable for any query whose underlying entity is `models.User`."""
    return query.filter(models.User.username != SYSTEM_USER_USERNAME)


def get_user(db: Session, user_id: int) -> Optional[models.User]:
    """Get a user by ID."""
    return db.query(models.User).filter(models.User.id == user_id).first()


def get_user_by_keycloak_id(db: Session, keycloak_user_id: str) -> Optional[models.User]:
    """Fetches a user from the local database by their Keycloak ID."""
    return db.query(models.User).filter(models.User.keycloak_user_id == keycloak_user_id).first()


def create_user(
    db: Session,
    user_details: schemas.UserCreate,
    tenant_id: int,
    parent_id: int,
    custom_role_id: Optional[int] = None
) -> models.User:
    """
    Creates a new user in the database within a specific tenant,
    assigning them a parent user and a role.
    """
    db_user = models.User(
        keycloak_user_id=user_details.keycloak_user_id,
        username=user_details.username,
        email=user_details.email,
        first_name=user_details.first_name,
        last_name=user_details.last_name,
        tenant_id=tenant_id,
        parent_user_id=parent_id,
        custom_role_id=custom_role_id,
        status=models.UserStatus.ACTIVE,
        onboarding_complete=True
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    # If user is assigned Admin role, also assign s3_tenant_admin Keycloak
    # group (dual-read alias: resolves minio_tenant_admin → s3_tenant_admin
    # in legacy realms without a live migration).
    if custom_role_id:
        role = db.query(models.CustomRole).filter(
            models.CustomRole.id == custom_role_id,
            models.CustomRole.deleted_at.is_(None)
        ).first()
        if role and role.name == "Admin" and db_user.keycloak_user_id:
            try:
                from utils.keycloak_client import assign_user_to_group
                assign_user_to_group(db_user.keycloak_user_id, "s3_tenant_admin")
            except Exception as e:
                if is_debug_mode():
                    logger.warning(f"Failed to assign user to s3_tenant_admin group: {e}")
                else:
                    logger.warning("Failed to assign user to s3_tenant_admin group")

    return db_user


def get_child_users(
    db: Session,
    parent_user_id: int,
    skip: int = 0,
    limit: int = 100
) -> List[models.User]:
    """Retrieve a paginated list of all child users for a specific parent user."""
    return (
        db.query(models.User)
        .filter(models.User.parent_user_id == parent_user_id)
        .filter(models.User.deleted_at.is_(None))
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_users_in_tenant(
    db: Session,
    tenant_id: int,
    skip: int = 0,
    limit: int = 100
) -> List[models.User]:
    """Retrieve a paginated list of all users within a specific tenant."""
    query = (
        db.query(models.User)
        .filter(models.User.tenant_id == tenant_id)
        .filter(models.User.keycloak_user_id.isnot(None))
        .filter(models.User.deleted_at.is_(None))
    )
    return (
        exclude_system_users(query)
        .offset(skip)
        .limit(limit)
        .all()
    )


def count_active_users_in_tenant(db: Session, tenant_id: int) -> int:
    """Counts the number of active users (seats used) in a tenant."""
    query = (
        db.query(models.User)
        .filter(models.User.tenant_id == tenant_id)
        .filter(models.User.keycloak_user_id.isnot(None))
        .filter(models.User.status == models.UserStatus.ACTIVE)
        .filter(models.User.deleted_at.is_(None))
    )
    return exclude_system_users(query).count()


def get_user_in_tenant(
    db: Session,
    user_id: int,
    tenant_id: int
) -> Optional[models.User]:
    """Retrieve a single user by their ID, ensuring they belong to the correct tenant."""
    return (
        db.query(models.User)
        .filter(models.User.id == user_id, models.User.tenant_id == tenant_id)
        .filter(models.User.deleted_at.is_(None))
        .first()
    )


def get_user_by_id_for_tenant(
    db: Session,
    user_id: int,
    tenant_id: int
) -> Optional[models.User]:
    """Retrieve a single user by ID, ensuring they belong to the correct tenant."""
    return (
        db.query(models.User)
        .filter(models.User.id == user_id, models.User.tenant_id == tenant_id)
        .filter(models.User.deleted_at.is_(None))
        .first()
    )


def update_user(
    db: Session,
    db_user: models.User,
    user_update: schemas.UserUpdate
) -> models.User:
    """Update a user's mutable fields, such as their role or status."""
    update_data = user_update.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_user, key, value)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def update_user_profile(
    db: Session,
    db_user: models.User,
    profile_update: schemas.UserProfileUpdate
) -> models.User:
    """Updates a user's profile attributes in the database."""
    update_data = profile_update.dict(exclude_unset=True)

    for key, value in update_data.items():
        setattr(db_user, key, value)

    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def revoke_user_api_keys(db: Session, user_id: int, reason: str = "deactivation") -> int:
    """Soft-revoke all live API keys owned by a user.

    ISO 27001 A.9.2.6 (removal of access rights): a deactivated/removed user
    must lose the ability to authenticate. API keys are SoftDeleteMixin rows
    (revocation == stamping `deleted_at`); once revoked, both the token-mint
    path (`get_api_key_by_key_prefix`) and request auth
    (`get_api_key_by_keycloak_client_id`) filter them out, so the key can no
    longer mint or validate.

    Emits an `AUTH_API_KEY_REVOKED` audit row per key with the `reason` in
    metadata (same action the manual revoke flow attributes). Returns the
    number of keys revoked. Call inside the caller's transaction — this
    stamps rows but does not commit.
    """
    from datetime import datetime, timezone
    keys = (
        db.query(models.ApiKey)
        .filter(
            models.ApiKey.user_id == user_id,
            models.ApiKey.deleted_at.is_(None),
        )
        .all()
    )
    if not keys:
        return 0
    # PermissionAuditLog.tenant_id is NOT NULL — attribute to the owner's
    # tenant (may differ from the actor's for a platform admin).
    owner = db.query(models.User).filter(models.User.id == user_id).first()
    tenant_id = owner.tenant_id if owner is not None else None
    now = datetime.now(timezone.utc)
    for key in keys:
        key.deleted_at = now
        # FX.78.p6 — WHY the key was revoked, so SCIM re-provisioning can
        # restore exactly the keys IT revoked (a key the user revoked
        # themselves stays revoked — ISO 27001 A.9.2.6).
        key.deleted_at_reason = reason
        if tenant_id is not None:
            try:
                from audit import log_permission_audit
                log_permission_audit(
                    db=db, action=models.AuditAction.AUTH_API_KEY_REVOKED,
                    actor_user_id=user_id, tenant_id=tenant_id,
                    resource_type="api_key", resource_id=key.id,
                    target_user_id=user_id,
                    metadata={"reason": reason, "key_prefix": key.key_prefix},
                )
            except Exception:
                pass
    return len(keys)


def restore_user_api_keys(db: Session, user_id: int, reason: str = "reactivation") -> int:
    """Soft-unrevoke (restore) all API keys that were revoked by SCIM
    deprovisioning, re-activating them for a re-activated user.

    ISO 27001 A.9.2.6: re-activation must restore prior access rights.
    Only keys that were revoked with `reason` containing "scim_deprovision"
    are restored (preserving the audit chain). Returns the number restored.
    """
    from datetime import datetime, timezone
    keys = (
        db.query(models.ApiKey)
        .filter(
            models.ApiKey.user_id == user_id,
            models.ApiKey.deleted_at.isnot(None),
            models.ApiKey.deleted_at_reason == "scim_deprovision",
        )
        .all()
    )
    if not keys:
        return 0
    # PermissionAuditLog.tenant_id is NOT NULL — attribute to the owner's
    # tenant (same rule the revoke path follows).
    owner = db.query(models.User).filter(models.User.id == user_id).first()
    tenant_id = owner.tenant_id if owner is not None else None
    now = datetime.now(timezone.utc)
    for key in keys:
        key.deleted_at = None
        key.deleted_at_reason = None
        try:
            from audit import log_permission_audit
            log_permission_audit(
                db=db, action=models.AuditAction.AUTH_API_KEY_REVOKED,
                actor_user_id=user_id, tenant_id=tenant_id,
                resource_type="api_key", resource_id=key.id,
                target_user_id=user_id,
                metadata={"reason": f"restored by SCIM re-activation ({reason})"},
            )
        except Exception:
            pass
    return len(keys)
    return len(keys)


def _revoke_user_api_keys(db: Session, user_id: int) -> int:
    """Soft-delete (revoke) all live API keys owned by a user.

    Delegates to the audited `revoke_user_api_keys`; kept as the original
    private name for the existing deactivate/delete call sites.
    """
    return revoke_user_api_keys(db, user_id, reason="deactivation")


def deactivate_user(db: Session, db_user: models.User) -> models.User:
    """Deactivates a user by setting their status.

    Also revokes the user's API keys so a deactivated account can no longer
    authenticate (previously the keys stayed live and could still mint OAuth
    tokens — ISO 27001 A.9.2.6 access-removal gap).
    """
    db_user.status = models.UserStatus.DEACTIVATED
    _revoke_user_api_keys(db, db_user.id)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def reactivate_user(db: Session, db_user: models.User) -> models.User:
    """Reactivate a previously-deactivated user (status -> ACTIVE).

    Restores login/API access at the account level. Any API keys revoked at
    deactivation stay revoked (soft-deleted) — the user mints fresh keys; we
    never silently un-revoke old credentials (ISO 27001 A.9.2.6).
    """
    db_user.status = models.UserStatus.ACTIVE
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def delete_user_permanently(db: Session, db_user: models.User) -> None:
    """Soft-delete the user row while scrubbing PII (GDPR + ISO 27001 style).

    A hard `DELETE FROM users` isn't viable: audit_events, credit ledgers,
    workflow_runs, and dozens of other tables FK to `users.id` and must
    remain attributable for ISO 27001 A.12.4 / CMMC AU-12 compliance. So
    instead we:

      1. Scrub PII on the row (email/first_name/last_name/username) so
         the row can't dox the ex-user — satisfies GDPR Art. 17.
      2. Stamp `deleted_at` so listing endpoints can filter it out.
      3. Set status=DEACTIVATED so any code paths still reading `status`
         treat the user as non-operational.
      4. Leave the row in place so every existing FK stays valid.

    Login is already prevented by the calling router deleting the user
    from Keycloak. The function keeps the name `delete_user_permanently`
    for router-ABI compatibility; the behaviour is now soft-delete.
    """
    from datetime import datetime, timezone

    if db_user.deleted_at is not None:
        # Idempotent: already soft-deleted.
        return

    # Clean up accepted invitations for this email in this tenant so the
    # scrub below doesn't leave unique-constraint dangling state.
    # ISO-exception: GDPR Art.17 erasure — Invitation has a DB-level UNIQUE(tenant_id, email) that also binds tombstones, so a soft-delete would keep the slot occupied and block re-invite of the same address; hard-delete required (acceptable within an erasure scrub)
    db.query(models.Invitation).filter(
        models.Invitation.email == db_user.email,
        models.Invitation.tenant_id == db_user.tenant_id,
        models.Invitation.status == models.InvitationStatus.ACCEPTED
    ).delete(synchronize_session=False)

    # Scrub PII. The `.invalid` TLD is reserved (RFC 2606), guaranteeing
    # these values never match a real address. The id suffix keeps uniqueness.
    uid = db_user.id
    db_user.email = f"deleted-user-{uid}@redacted.invalid"
    db_user.username = f"deleted-user-{uid}"
    db_user.first_name = "Deleted"
    db_user.last_name = "User"
    db_user.keycloak_user_id = None
    db_user.status = models.UserStatus.DEACTIVATED
    db_user.deleted_at = datetime.now(timezone.utc)
    # GSEC R-016 / FX.15 — anchor the privacy-erasure grace window at the
    # deletion moment unless the user had already requested it (the
    # account-deletion endpoint stamps deletion_requested_at earlier).
    if db_user.deletion_requested_at is None:
        db_user.deletion_requested_at = db_user.deleted_at

    # Revoke API keys so the removed account can no longer authenticate
    # (ISO 27001 A.9.2.6). Keys are soft-deleted, preserving the audit trail.
    _revoke_user_api_keys(db, db_user.id)

    db.add(db_user)
    db.commit()


def get_user_teams(
    db: Session,
    user_id: int,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "name",
    order: str = "asc",
) -> List[models.Team]:
    """Retrieve the teams a specific user is a member of, with pagination and sorting."""
    query = (
        db.query(models.Team)
        .join(models.team_members_association)
        .filter(
            models.team_members_association.c.user_id == user_id,
            models.Team.deleted_at.is_(None)
        )
    )

    if hasattr(models.Team, sort_by):
        column = getattr(models.Team, sort_by)
        if order.lower() == "desc":
            query = query.order_by(column.desc())
        else:
            query = query.order_by(column.asc())

    return query.offset(offset).limit(limit).all()


def get_user_assigned_tasks(
    db: Session,
    user_id: int,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "assigned_at",
    order: str = "desc",
) -> List[models.ManualInterventionState]:
    """Retrieve pending manual intervention tasks assigned to a specific user."""
    query = db.query(models.ManualInterventionState).filter(
        models.ManualInterventionState.assigned_to_user_id == user_id,
        models.ManualInterventionState.status == models.InterventionStatus.PENDING_ACTION
    )

    if hasattr(models.ManualInterventionState, sort_by):
        column = getattr(models.ManualInterventionState, sort_by)
        if order.lower() == "desc":
            query = query.order_by(column.desc())
        else:
            query = query.order_by(column.asc())

    return query.offset(offset).limit(limit).all()


def get_user_by_api_key(db: Session, api_key: str) -> Optional[models.User]:
    """Retrieves a user by their API key."""
    return db.query(models.User).join(models.ApiKey).filter(models.ApiKey.key_prefix == api_key[:8]).first()
