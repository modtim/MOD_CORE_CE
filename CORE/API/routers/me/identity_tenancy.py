"""Identity/tenancy domain for the current user — profile, preferences, GDPR
data-export / account-deletion, and session listing/revocation."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services._base import ConflictError, NotFoundError
from services.me_identity_service import me_identity_service, _iso, _enum  # noqa: F401 -- _iso/_enum re-exported via routers.me.__init__

from . import ProfileUpdate, EmailPreferences, NotificationPreferencesUpdate

router = APIRouter()


@router.put("/profile")
def update_profile(
    data: ProfileUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Update the current user's profile (name fields)."""
    return me_identity_service.update_profile(db, current_user, data)


# FX.140 part 2 (Q128=A) — GET/PUT /v1/me/email-preferences is served by
# routers/email_preferences.py over the append-only consent record (the
# richer, newer handler; it also answers the legacy
# `lifecycle_emails_opted_out` shape the Dashboard/MOBILE send). This
# router's duplicate registration of the same (method, path) pair was
# shadowed dead code (FastAPI serves the first mount only) and is removed
# (same WS1.40d dedupe pattern as routers/private.py +
# routers/capabilities.py). The plain helpers below stay: routers/me
# re-exports them and the legacy flag is still the activation-lifecycle
# sweep gate (utils/activation_lifecycle.py), which email_preferences.py
# keeps in sync on the back-compat PUT shape.
def get_email_preferences(
    current_user: models.User,
) -> dict:
    """Read the current user's lifecycle-email opt-out preference (helper,
    NOT a route — the route lives in routers/email_preferences.py)."""
    return me_identity_service.get_email_preferences(current_user)


def set_email_preferences(
    data: EmailPreferences,
    db: Session,
    current_user: models.User,
) -> dict:
    """Update the current user's legacy lifecycle-email opt-out flag (helper,
    NOT a route — the route lives in routers/email_preferences.py)."""
    return me_identity_service.set_email_preferences(db, current_user, data)


@router.get("/notification-preferences")
def get_notification_preferences(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """WS4.Q22 — read the current user's per-category (workflow / billing /
    product), per-channel (in_app / email) notification preferences. A
    category/channel with no stored row is reported as enabled (True)."""
    return me_identity_service.get_notification_preferences(db, current_user)


@router.put("/notification-preferences")
def set_notification_preferences(
    data: NotificationPreferencesUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """WS4.Q22 — update the current user's notification preferences (partial:
    only categories/channels present in the payload change). Returns the
    full resulting preference map."""
    return me_identity_service.set_notification_preferences(db, current_user, data)


@router.get("/permissions")
def get_my_permissions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Retrieves the permissions/capabilities of the currently authenticated user.
    Platform admins get all permissions (god mode).

    Returns capabilities in a backward-compatible format with 'name' field
    formatted as 'resource_type:capability_name' (e.g., 'secret:create').
    """
    return me_identity_service.get_my_permissions(db, current_user)


# ---------------------------------------------------------------------------
# GDPR Compliance Endpoints
# ---------------------------------------------------------------------------


@router.get("/data-export", summary="Export all personal data (GDPR Art. 20)")
def export_personal_data(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Export ALL data associated with the current user in a portable JSON format.
    GDPR Article 20 — Right to Data Portability.

    Everything is scoped to the requesting user (owner / actor / recipient),
    always within the user's own tenant. All secret material — API-key hashes,
    Keycloak client secrets, secret values, tokens — is redacted; only
    non-sensitive references/metadata are exported.
    """
    return me_identity_service.export_personal_data(db, current_user)


@router.delete("/account", status_code=status.HTTP_200_OK,
               summary="Request account deletion (GDPR Art. 17)")
def request_account_deletion(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Request deletion of the current user's account.
    GDPR Article 17 — Right to Erasure.

    The account is immediately deactivated. Billing records are anonymized
    but retained for legal/tax compliance (typically 7 years).
    """
    try:
        return me_identity_service.request_account_deletion(db, current_user)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message)


# --- Session Management ---

@router.get("/sessions")
def list_sessions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """List active sessions for the current user."""
    return me_identity_service.list_sessions(db, current_user)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Revoke a specific session."""
    try:
        me_identity_service.revoke_session(db, current_user, session_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
