"""CRUD (query-only) layer for the `routers/invitations.py` domain (Gate 2b
migration).

Every function here is a plain SQLAlchemy read — no policy, no HTTP, no
external I/O. `services/invitations_service.py` composes these (plus the
pre-existing `crud/invitation_cud.py` functions re-exported off the top-level
`crud` package: `create_invitation`, `get_invitation_by_id`,
`delete_invitation`, `get_pending_invitations_for_tenant`,
`get_pending_invitation_by_token`) into business logic;
`routers/invitations.py` calls down into the service instead of running
`db.query(...)` inline.

This module is intentionally separate from `crud/invitation_cud.py` (per the
migration's shared rules — "add new query fns to crud/invitations.py", reusing
the existing invitation CRUD where present) rather than folding these into
that file, mirroring the sibling `crud/external_invitations.py` split done for
the same domain family.

404-vs-403 / tenant-scoping notes (moved verbatim from the pre-migration
router, not changed here):
  * `get_tenant_by_id` is tenant-unscoped by construction (a `Tenant` row has
    no `tenant_id` column) — matches the original inline
    `db.query(models.Tenant).filter(models.Tenant.id == target_tenant_id)`
    call sites in `create_and_send_invitation`.
  * `get_user_by_email` deliberately searches ALL tenants (no tenant filter)
    — the original inline query in `create_and_send_invitation` needs to know
    whether the email exists ANYWHERE on the platform to decide between the
    "already a member of this tenant" / cross-tenant-oracle-safe / platform-
    admin-truth branches (see the service docstring). This is a policy
    decision the *service* makes from the result, not a query bug.
  * `get_invitation_by_id_excluding_deleted` is tenant-UNSCOPED on purpose —
    matching the original `cancel_invitation` query. The service applies the
    tenant check itself and raises 403 (not 404) on a cross-tenant id, so a
    legitimate 403 doesn't collapse into a 404 (the migration's documented
    404-vs-403 trap).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_tenant_by_id(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    """Fetch a tenant by id. No `deleted_at` filter — matches the original
    inline queries (both call sites in `create_and_send_invitation`) which
    had none either."""
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


def get_user_by_email(db: Session, email: str) -> Optional[models.User]:
    """Fetch a LIVE user by email, across ALL tenants (no tenant scoping) —
    matches the original inline existence check in
    `create_and_send_invitation`, plus the BUG.B7 `deleted_at` filter.

    Soft-deleted (GDPR-erasure) rows are excluded on purpose: `crud.user_cud.
    delete_user_permanently` scrubs the row's email to
    `deleted-user-<id>@redacted.invalid` and nulls `keycloak_user_id`, so a
    tombstone no longer owns the address and there is no account to
    "reactivate" — re-inviting that email must create a fresh user + fresh
    Keycloak account. Keeping the tombstone in the dup check produced a 409
    for an email the owner could neither see nor delete.
    """
    return (
        db.query(models.User)
        .filter(models.User.email == email)
        .filter(models.User.deleted_at.is_(None))
        .first()
    )


def get_pending_invitation_by_tenant_and_email(
    db: Session, tenant_id: int, email: str
) -> Optional[models.Invitation]:
    """Fetch a non-soft-deleted, PENDING invitation for this tenant + email —
    matches the original duplicate-invitation guard in
    `create_and_send_invitation`."""
    return (
        db.query(models.Invitation)
        .filter(
            models.Invitation.email == email,
            models.Invitation.tenant_id == tenant_id,
            models.Invitation.status == models.InvitationStatus.PENDING,
            models.Invitation.deleted_at.is_(None),
        )
        .first()
    )


def get_invitation_by_id_excluding_deleted(
    db: Session, invitation_id: int
) -> Optional[models.Invitation]:
    """Fetch a non-soft-deleted invitation by id. Tenant-UNSCOPED — see
    module docstring (the service enforces the tenant check so a
    cross-tenant id yields 403, not 404)."""
    return (
        db.query(models.Invitation)
        .filter(
            models.Invitation.id == invitation_id,
            models.Invitation.deleted_at.is_(None),
        )
        .first()
    )
