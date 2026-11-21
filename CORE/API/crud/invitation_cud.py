"""Invitation CRUD operations - Invitation management within tenants."""
from utils.pii_redaction import mask_email

from typing import Optional, List

from sqlalchemy.orm import Session
from datetime import datetime, timedelta

import models, schemas
import secrets


def create_invitation(
    db: Session,
    invitation: schemas.InvitationCreate,
    inviter: models.User,
    target_tenant_id: int = None
) -> models.Invitation:
    """
    Creates a new invitation record in the database, or returns an existing pending one.
    
    Args:
        target_tenant_id: If provided (by platform admin), invitation is created for this tenant.
                          Otherwise uses inviter's tenant.
    """
    # Determine which tenant to use
    tenant_id = target_tenant_id if target_tenant_id is not None else inviter.tenant_id

    # iso-soft-delete-sweep (2026-09-22): the (tenant_id, email) uniqueness is a
    # plain constraint, so at most ONE row ever exists per (tenant, email) —
    # including soft-deleted (cancelled) tombstones. Look it up WITH tombstones
    # so a re-invite reactivates the existing row instead of colliding on INSERT.
    existing_invitation = (
        db.query(models.Invitation)
        .execution_options(include_deleted=True)
        .filter(
            models.Invitation.tenant_id == tenant_id,
            models.Invitation.email == invitation.email.lower(),
        )
        .first()
    )

    if (
        existing_invitation is not None
        and existing_invitation.deleted_at is None
        and existing_invitation.status == models.InvitationStatus.PENDING
    ):
        from logging import getLogger
        logger = getLogger(__name__)
        logger.info(
            f"Invitation already exists for {mask_email(invitation.email)} in tenant {tenant_id}. Returning existing invitation.")
        return existing_invitation

    # Generate a secure, URL-safe token for the new invitation
    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(days=7)  # Invitations expire in 7 days

    if existing_invitation is not None and existing_invitation.deleted_at is not None:
        # Re-invite after a soft-deleted (cancelled) invitation: reactivate the
        # tombstone in place so the unique slot is reused.
        existing_invitation.deleted_at = None
        existing_invitation.status = models.InvitationStatus.PENDING
        existing_invitation.invited_by_user_id = inviter.id
        existing_invitation.custom_role_id = invitation.custom_role_id
        existing_invitation.token = token
        existing_invitation.expires_at = expires_at
        existing_invitation.initial_cog_id = invitation.cog_id
        existing_invitation.initial_permission_template_id = invitation.template_id
        existing_invitation.initial_permission_level = invitation.permission_level
        db.add(existing_invitation)
        db.commit()
        db.refresh(existing_invitation)
        return existing_invitation

    db_invitation = models.Invitation(
        email=invitation.email.lower(),
        tenant_id=tenant_id,
        invited_by_user_id=inviter.id,
        custom_role_id=invitation.custom_role_id,
        token=token,
        expires_at=expires_at,
        status=models.InvitationStatus.PENDING,
        # Save the optional initial permissions
        initial_cog_id=invitation.cog_id,
        initial_permission_template_id=invitation.template_id,
        initial_permission_level=invitation.permission_level  # Kept for backward compatibility
    )

    db.add(db_invitation)
    db.commit()
    db.refresh(db_invitation)

    return db_invitation


def accept_invitation(
    db: Session,
    db_invitation: models.Invitation,
    accepted_by_user_id: int = None
) -> models.Invitation:
    """
    Marks an invitation as accepted and creates a direct permission grant if cog_id is set.
    """
    db_invitation.status = models.InvitationStatus.ACCEPTED
    db.add(db_invitation)

    # Create the permission grant from the invitation if cog_id is set
    if db_invitation.initial_cog_id and accepted_by_user_id:
        # If template is specified, use it to apply permissions
        if db_invitation.initial_permission_template_id:
            template = db.query(models.PermissionTemplate).filter(
                models.PermissionTemplate.id == db_invitation.initial_permission_template_id
            ).first()

            if template:
                from permissions import create_permission
                from datetime import datetime, timedelta

                expires_at = None
                if template.duration_days:
                    expires_at = datetime.utcnow() + timedelta(days=template.duration_days)

                create_permission(
                    db=db,
                    cog_id=db_invitation.initial_cog_id,
                    level=models.PermissionLevel(template.permission_level),
                    granted_by_user_id=db_invitation.invited_by_user_id,
                    user_id=accepted_by_user_id,
                    expires_at=expires_at,
                    can_upload=template.can_upload,
                    can_create_subfolders=template.can_create_subfolders,
                    can_move_files=template.can_move_files,
                    can_process=template.can_process,
                    can_share=template.can_share,
                    can_delete=template.can_delete,
                    is_external_user=True
                )
        # Fall back to manual permission_level if no template
        elif db_invitation.initial_permission_level:
            from permissions import create_permission

            create_permission(
                db=db,
                cog_id=db_invitation.initial_cog_id,
                level=db_invitation.initial_permission_level,
                granted_by_user_id=db_invitation.invited_by_user_id,
                user_id=accepted_by_user_id,
                is_external_user=True
            )

    db.commit()
    db.refresh(db_invitation)
    return db_invitation


def get_invitations_for_tenant(
    db: Session,
    tenant_id: int
) -> List[models.Invitation]:
    """Retrieve all pending invitations for a specific tenant."""
    return (
        db.query(models.Invitation)
        .filter(models.Invitation.tenant_id == tenant_id, models.Invitation.status == models.InvitationStatus.PENDING)
        .all()
    )


def get_invitation_by_id_for_tenant(
    db: Session,
    invitation_id: int,
    tenant_id: int
) -> Optional[models.Invitation]:
    """Retrieve a single invitation by its ID, ensuring it belongs to the correct tenant."""
    return (
        db.query(models.Invitation)
        .filter(
            models.Invitation.id == invitation_id,
            models.Invitation.tenant_id == tenant_id
        )
        .first()
    )


def get_invitation_by_id(db: Session, invitation_id: int) -> Optional[models.Invitation]:
    """Retrieve a single invitation by its ID without tenant filtering."""
    return (
        db.query(models.Invitation)
        .filter(models.Invitation.id == invitation_id)
        .first()
    )


def delete_invitation(db: Session, db_invitation: models.Invitation):
    """Soft-delete (cancel) an invitation record.

    iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    iso-no-hard-delete). `Invitation` carries SoftDeleteMixin and is in
    database.py's soft-delete listener, so the tombstoned invitation drops out
    of live reads. `create_invitation` reactivates the tombstone on re-invite
    (the (tenant_id, email) uniqueness is a plain constraint, so the row still
    owns the slot).
    """
    db_invitation.deleted_at = datetime.utcnow()
    db.commit()


def get_pending_invitations_for_tenant(
    db: Session,
    tenant_id: int,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "expires_at",
    order: str = "asc",
) -> List[models.Invitation]:
    """Retrieve pending invitations for a specific tenant with pagination and sorting."""
    query = db.query(models.Invitation).filter(
        models.Invitation.tenant_id == tenant_id,
        models.Invitation.status == models.InvitationStatus.PENDING
    )

    # Apply sorting
    if hasattr(models.Invitation, sort_by):
        column = getattr(models.Invitation, sort_by)
        if order.lower() == "desc":
            query = query.order_by(column.desc())
        else:
            query = query.order_by(column.asc())

    return query.offset(offset).limit(limit).all()


def get_pending_invitation_by_token(db: Session, token: str) -> Optional[models.Invitation]:
    """Retrieve a single, active, and non-expired invitation by its unique token."""
    return (
        db.query(models.Invitation)
        .filter(
            models.Invitation.token == token,
            models.Invitation.status == models.InvitationStatus.PENDING,
            models.Invitation.expires_at > datetime.utcnow()
        )
        .first()
    )
