"""GX.A3b — per-app non-human service user (owner decision OQ-2=b).

Every App gets exactly one backing ``users`` row — a *service account* that
the app's scoped API key authenticates as (``api_keys.user_id`` points at it).
It is created alongside the app and soft-deleted when the app is revoked.

Design constraints:
  * NO interactive login: the row has no password and no Keycloak user
    credentials (``keycloak_user_id`` stays NULL). Interactive (Bearer)
    resolution of a service account is refused upstream (dependencies.get_
    current_user raises 403).
  * Deterministic identity: username ``app-<id>`` / email
    ``app-<id>@apps.invalid``. The ``.invalid`` TLD (RFC 2606) guarantees the
    address can never receive or match mail.
  * Lowest-privilege member role: default tenant member (``custom_role_id``
    NULL), ``status=ACTIVE``, ``onboarding_complete=True`` — enough to run
    in-scope workflows, nothing more.
  * ``is_service_account=True`` marks it (the column predates this task —
    migration 20260513).
  * ISO 27001: NEVER hard-deleted. Revocation soft-deletes (``deleted_at``)
    and deactivates the status, keeping the row intact for audit attribution.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)


def service_account_email(app_id: int) -> str:
    """Deterministic, never-routable email for app ``app_id``."""
    return f"app-{app_id}@apps.invalid"


def service_account_username(app_id: int) -> str:
    """Deterministic username for app ``app_id``."""
    return f"app-{app_id}"


def is_app_service_user(user: Optional[models.User]) -> bool:
    """True when ``user`` is an app's backing service account."""
    if user is None:
        return False
    return bool(getattr(user, "is_service_account", False))


def _find_service_user(db: Session, app: models.App) -> Optional[models.User]:
    return (
        db.query(models.User)
        .filter(models.User.email == service_account_email(app.id))
        .first()
    )


def ensure_app_service_user(db: Session, app: models.App) -> models.User:
    """Idempotently create (or re-activate) the service user for ``app``.

    Returns the ``User`` row. A pre-existing row — live or previously
    soft-deleted — is reused; we never create a second one (email is unique).
    """
    email = service_account_email(app.id)
    username = service_account_username(app.id)

    user = _find_service_user(db, app)
    if user is None:
        user = models.User(
            username=username,
            email=email,
            first_name="App",
            last_name=f"Service {app.id}",
            tenant_id=app.tenant_id,
            # No Keycloak identity: this account cannot log in interactively.
            keycloak_user_id=None,
            # Parent it to whoever provisioned the app, for the tenant tree.
            parent_user_id=getattr(app, "created_by_user_id", None),
            # Lowest-privilege tenant member — no custom role.
            custom_role_id=None,
            status=models.UserStatus.ACTIVE,
            onboarding_complete=True,
            is_service_account=True,
            provisioning_source="app",
            is_email_verified=True,  # non-human; never gates on mail
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(
            "created app service user: app=%s user=%s tenant=%s",
            app.id, user.id, app.tenant_id,
        )
        return user

    # Idempotent path: reuse + heal (e.g. re-activation after a revoke).
    changed = False
    if user.tenant_id != app.tenant_id:
        user.tenant_id = app.tenant_id
        changed = True
    if user.is_service_account is not True:
        user.is_service_account = True
        changed = True
    if user.status != models.UserStatus.ACTIVE:
        user.status = models.UserStatus.ACTIVE
        changed = True
    if user.deleted_at is not None:
        user.deleted_at = None
        changed = True
    if changed:
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def revoke_app_service_user(db: Session, app: models.App) -> Optional[models.User]:
    """Soft-delete the app's service user. NEVER hard-deletes (ISO 27001).

    Sets ``deleted_at`` and deactivates the status so the row stays intact for
    audit attribution while the account can no longer authenticate. Returns
    the row (or None if the app never had one).
    """
    user = _find_service_user(db, app)
    if user is None:
        return None
    user.deleted_at = datetime.now(timezone.utc)
    user.status = models.UserStatus.DEACTIVATED
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(
        "revoked app service user: app=%s user=%s (soft-delete)",
        app.id, user.id,
    )
    return user
