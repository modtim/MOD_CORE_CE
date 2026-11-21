"""CRUD data-access for the ``/me`` identity-tenancy domain (Gate 2b).

The ONLY place ``db.query(...)`` lives for this domain: profile/permissions,
GDPR data-export (Art. 20) / account-deletion (Art. 17), and session
listing/revocation. ``routers/me/identity_tenancy.py`` and
``services/me_identity_service.py`` reach for these functions instead of
hand-writing inline queries.

Behaviour-preserving Gate-2 migration: every function is a straight lift of a
previous inline router query, relocated — not a redesign. Several of these
queries are intentionally tenant/user-scoped WITHOUT a soft-delete-aware
`CRUDBase` (the domain mixes plain, soft-deleted and column-projected models),
so this module uses plain functions matching the existing `crud/*.py` style
(e.g. `crud/notifications.py`).
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

import models


# =============================================================================
# Permissions ("/me/permissions")
# =============================================================================


def list_all_capabilities(db: Session) -> List["models.Capability"]:
    """Every capability row — used for the platform-admin 'god mode' list."""
    return db.query(models.Capability).all()


def get_role_with_capabilities(
    db: Session, custom_role_id: int
) -> Optional["models.CustomRole"]:
    """A non-soft-deleted custom role, eager-loading its capabilities."""
    return (
        db.query(models.CustomRole)
        .options(joinedload(models.CustomRole.capabilities))
        .filter(
            models.CustomRole.id == custom_role_id,
            models.CustomRole.deleted_at.is_(None),
        )
        .first()
    )


# =============================================================================
# GDPR data export ("/me/data-export") — one function per exported section
# =============================================================================


def get_permission_audit_for_user(
    db: Session, tenant_id: int, user_id: int
) -> List["models.PermissionAuditLog"]:
    """Grants/revokes/requests where the user is actor OR target, newest first,
    capped at 1000 rows."""
    return (
        db.query(models.PermissionAuditLog)
        .filter(
            models.PermissionAuditLog.tenant_id == tenant_id,
            (
                (models.PermissionAuditLog.actor_user_id == user_id)
                | (models.PermissionAuditLog.target_user_id == user_id)
            ),
        )
        .order_by(models.PermissionAuditLog.timestamp.desc())
        .limit(1000)
        .all()
    )


def get_permission_grants_for_user(
    db: Session, tenant_id: int, user_id: int
) -> List["models.ResourcePermission"]:
    """Permission grants the user holds OR issued, tenant-scoped."""
    return (
        db.query(models.ResourcePermission)
        .filter(
            models.ResourcePermission.tenant_id == tenant_id,
            (
                (models.ResourcePermission.user_id == user_id)
                | (models.ResourcePermission.granted_by_user_id == user_id)
            ),
        )
        .all()
    )


def get_permission_requests_for_user(
    db: Session, tenant_id: int, user_id: int
) -> List["models.PermissionRequest"]:
    """Permission requests the user filed, newest first."""
    return (
        db.query(models.PermissionRequest)
        .filter(
            models.PermissionRequest.tenant_id == tenant_id,
            models.PermissionRequest.requester_id == user_id,
        )
        .order_by(models.PermissionRequest.created_at.desc())
        .all()
    )


def get_workflows_owned(db: Session, tenant_id: int, user_id: int) -> List[Any]:
    """Explicit columns (not whole ORM objects) — immune to unrelated column
    drift on the workflows table, matching the original inline query."""
    return (
        db.query(
            models.Workflow.id,
            models.Workflow.name,
            models.Workflow.is_system,
            models.Workflow.created_by_user_id,
        )
        .filter(
            models.Workflow.tenant_id == tenant_id,
            models.Workflow.created_by_user_id == user_id,
            models.Workflow.deleted_at.is_(None),
        )
        .all()
    )


def get_cogs_owned(db: Session, tenant_id: int, user_id: int) -> List["models.Cog"]:
    """Non-soft-deleted files/cogs owned by the user."""
    return (
        db.query(models.Cog)
        .filter(
            models.Cog.tenant_id == tenant_id,
            models.Cog.owner_id == user_id,
            models.Cog.deleted_at.is_(None),
        )
        .all()
    )


def get_active_api_keys(db: Session, user_id: int) -> List["models.ApiKey"]:
    """Non-soft-deleted API keys for a user — shared by the data-export
    endpoint and account deletion (which revokes them)."""
    return (
        db.query(models.ApiKey)
        .filter(
            models.ApiKey.user_id == user_id,
            models.ApiKey.deleted_at.is_(None),
        )
        .all()
    )


def get_secrets_owned(db: Session, tenant_id: int, user_id: int) -> List["models.Secret"]:
    """Non-soft-deleted secrets created by the user, tenant-scoped."""
    return (
        db.query(models.Secret)
        .filter(
            models.Secret.tenant_id == tenant_id,
            models.Secret.created_by_user_id == user_id,
            models.Secret.deleted_at.is_(None),
        )
        .all()
    )


def get_invitations_sent(db: Session, tenant_id: int, user_id: int) -> List["models.Invitation"]:
    """Invitations the user sent, tenant-scoped."""
    return (
        db.query(models.Invitation)
        .filter(
            models.Invitation.tenant_id == tenant_id,
            models.Invitation.invited_by_user_id == user_id,
        )
        .all()
    )


def get_invitations_received(db: Session, email: str) -> List["models.Invitation"]:
    """Invitations addressed to the user's current email (NOT tenant-scoped —
    an invitation targets an email before the invitee has a tenant)."""
    return db.query(models.Invitation).filter(models.Invitation.email == email).all()


def get_subscription_for_tenant(db: Session, tenant_id: int) -> Optional["models.Subscription"]:
    """The tenant's subscription row, if any."""
    return db.query(models.Subscription).filter(models.Subscription.tenant_id == tenant_id).first()


def get_credit_ledger_for_user(
    db: Session, tenant_id: int, user_id: int
) -> List["models.CreditLedgerEntry"]:
    """Credit ledger entries attributed to the user, newest first, capped at
    1000 rows."""
    return (
        db.query(models.CreditLedgerEntry)
        .filter(
            models.CreditLedgerEntry.tenant_id == tenant_id,
            models.CreditLedgerEntry.user_id == user_id,
        )
        .order_by(models.CreditLedgerEntry.created_at.desc())
        .limit(1000)
        .all()
    )


def get_notifications_for_user(db: Session, user_id: int) -> List["models.Notification"]:
    """The user's notifications, newest first, capped at 1000 rows."""
    return (
        db.query(models.Notification)
        .filter(models.Notification.recipient_user_id == user_id)
        .order_by(models.Notification.created_at.desc())
        .limit(1000)
        .all()
    )


def get_audit_events_for_user(db: Session, user_id: int) -> List["models.AuditEvent"]:
    """Login/audit events for the user, newest first, capped at 1000 rows."""
    return (
        db.query(models.AuditEvent)
        .filter(models.AuditEvent.user_id == user_id)
        .order_by(models.AuditEvent.timestamp.desc())
        .limit(1000)
        .all()
    )


def count_legal_hold_outputs(db: Session, tenant_id: int) -> int:
    """Count of tenant node outputs currently under legal hold — gates account
    deletion (#113)."""
    from models import WorkflowNodeOutput

    return (
        db.query(WorkflowNodeOutput)
        .filter(
            WorkflowNodeOutput.tenant_id == tenant_id,
            WorkflowNodeOutput.legal_hold == True,  # noqa: E712
        )
        .count()
    )


# =============================================================================
# Sessions ("/me/sessions")
# =============================================================================


def get_sessions_for_user(db: Session, user_id: int) -> List["models.UserSession"]:
    """A user's sessions, most-recently-active first. Shared by the
    data-export section and the sessions-list endpoint."""
    return (
        db.query(models.UserSession)
        .filter(models.UserSession.user_id == user_id)
        .order_by(models.UserSession.last_active_at.desc())
        .all()
    )


def get_session_for_user(
    db: Session, session_id: int, user_id: int
) -> Optional["models.UserSession"]:
    """Fetch one session, scoped to its owner (never cross-user)."""
    return (
        db.query(models.UserSession)
        .filter(
            models.UserSession.id == session_id,
            models.UserSession.user_id == user_id,
        )
        .first()
    )


def get_enterprise_leads_for_user(
    db: Session, email: str
) -> List["models.EnterpriseLead"]:
    """All enterprise leads submitted by the given email address (case-insensitive).
    Used for GDPR data export (Art. 20) to return leads created by the user."""
    return (
        db.query(models.EnterpriseLead)
        .filter(models.EnterpriseLead.email.ilike(email))
        .order_by(models.EnterpriseLead.created_at.desc())
        .all()
    )
