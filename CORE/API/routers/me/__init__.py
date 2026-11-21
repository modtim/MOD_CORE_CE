"""Current-user ("/me") endpoints, split into per-domain sub-modules.

Sub-modules: identity_tenancy, iam_permissions, auth_apikeys. The request
models are DEFINED here so their ``__module__`` stays ``routers.me`` (identical
to the pre-split single file), keeping OpenAPI ``$ref`` names unchanged. The
sub-module routers are re-assembled into the same aggregate ``router`` (same
tags), so ``main.py`` is untouched.
"""

from typing import Optional

import schemas, crud, models
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dependencies import get_db, get_current_user, require_active_and_onboarded_user

router = APIRouter(tags=["Me"])


def _fx54_session_tier(db: Session, user: models.User) -> dict:
    """GSEC FX.54 part 2 — effective session tier for this caller. Never
    raises (the service returns defaults on a settings-read failure)."""
    from services import session_tier_enforcement as _fx54
    return _fx54.tier_for_response(db, user)


class ProfileUpdate(schemas.BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None


class EmailPreferences(schemas.BaseModel):
    # activation-lifecycle-emails: opt out of activation/marketing lifecycle
    # mail. Transactional mail (verify, billing, security) is never affected.
    lifecycle_emails_opted_out: bool


class NotificationChannelPreference(schemas.BaseModel):
    """notification-preferences (WS4.Q22): per-channel toggle for one
    category. Unset fields leave that channel's stored preference
    untouched (partial update)."""
    in_app: Optional[bool] = None
    email: Optional[bool] = None


class NotificationPreferencesUpdate(schemas.BaseModel):
    """notification-preferences (WS4.Q22): PUT body for
    ``/me/notification-preferences``. Matches the account-settings
    "Notification preferences" card categories (Workflow run updates,
    Billing & payments, Product & release updates). Any category may be
    omitted to leave it unchanged."""
    workflow: Optional[NotificationChannelPreference] = None
    billing: Optional[NotificationChannelPreference] = None
    product: Optional[NotificationChannelPreference] = None


# The two root ("/me") endpoints are registered directly on the aggregate router
# (identity_tenancy domain): FastAPI forbids a nested-included route whose prefix
# AND path are both empty, and the aggregate router carries no prefix (main.py
# supplies "/me" at include time). Every other endpoint lives in a sub-module.

@router.get("", response_model=schemas.User)
def get_current_user_profile(
    db: Session = Depends(get_db),
    # This endpoint now uses the base get_current_user dependency, which allows
    # it to succeed even if the user is not fully onboarded.
    current_user: models.User = Depends(get_current_user)
):
    """
    Retrieves the profile of the currently authenticated user.
    Includes computed fields like has_custom_registry_access, seats_used, and seats_limit.
    """
    # Compute whether user has access to custom Docker registry
    # Platform admins always have access, OR tenant 1 parent users, OR users with enterprise plans
    is_platform_tenant_admin = current_user.tenant_id == 1 and current_user.parent_user_id is None
    has_custom_registry = current_user.is_platform_admin or is_platform_tenant_admin

    # Compute seat usage and storage information
    seats_used = None
    seats_limit = None
    storage_used_bytes = None
    storage_limit_bytes = None

    if current_user.tenant:
        # Count active users in the tenant
        seats_used = crud.count_active_users_in_tenant(db, current_user.tenant_id)

        # Get current storage usage
        storage_used_bytes = current_user.tenant.storage_used_bytes or 0

        # Get limits from plan (if tenant has a subscription)
        if current_user.tenant.subscription and current_user.tenant.subscription.plan:
            subscription = current_user.tenant.subscription
            plan = subscription.plan

            # Get seat limit
            seats_limit = plan.max_seats

            # Get storage limit (convert GB to bytes). NULL = unlimited.
            # Paid-only: leave storage_limit_bytes as None (= unlimited)
            # when the module is absent on CE — never 402 here.
            from utils.paid_import import optional_paid
            _slim = optional_paid("utils.storage_limits")
            if _slim is not None:
                storage_limit_bytes = _slim.storage_limit_bytes_from_plan(plan)

            # Check custom registry access via the central entitlement gate
            if not has_custom_registry:
                from utils import entitlements
                has_custom_registry = entitlements.evaluate_feature(
                    entitlements.Cap.CUSTOM_REGISTRY,
                    entitlements.get_entitlements(db, current_user.tenant),
                ).allowed

    # Platform admin on the platform tenant: no caps. None is the "unlimited"
    # sentinel — frontends render "Unlimited" instead of a number.
    if current_user.is_platform_admin and current_user.tenant_id == 1:
        seats_limit = None
        storage_limit_bytes = None

    # Add the computed fields to the response
    # We need to convert to dict and add the field since it's not in the DB model
    user_dict = {
        **{c.name: getattr(current_user, c.name) for c in current_user.__table__.columns},
        'custom_role': current_user.custom_role,
        'has_custom_registry_access': has_custom_registry,
        'seats_used': seats_used,
        'seats_limit': seats_limit,
        'storage_used_bytes': storage_used_bytes,
        'storage_limit_bytes': storage_limit_bytes,
        # GSEC FX.54 part 2 — the caller's effective session tier (idle / max /
        # warning lead) for the idle-warning dialog.
        'session_tier': _fx54_session_tier(db, current_user),
    }

    return user_dict


@router.patch("", response_model=schemas.User)
def update_current_user_profile(
    profile_update: schemas.UserProfileUpdate,
    db: Session = Depends(get_db),
    # The other endpoints correctly use the stricter dependency.
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Updates the profile of the currently authenticated user.
    """
    return crud.update_user_profile(db=db, db_user=current_user, profile_update=profile_update)


from . import identity_tenancy, iam_permissions, auth_apikeys, mfa  # noqa: E402

router.include_router(identity_tenancy.router)
router.include_router(iam_permissions.router)
router.include_router(auth_apikeys.router)
# GY.C4.MFA — 2FA status + MOD-branded enrollment URL (GET /v1/me/mfa).
router.include_router(mfa.router)

# Backward-compat re-exports (callers/tests import these off routers.me)
from .identity_tenancy import (  # noqa: E402,F401
    update_profile,
    get_email_preferences,
    set_email_preferences,
    get_notification_preferences,
    set_notification_preferences,
    get_my_permissions,
    export_personal_data,
    request_account_deletion,
    list_sessions,
    revoke_session,
    _iso,
    _enum,
)
from .iam_permissions import (  # noqa: E402,F401
    get_my_teams,
    get_my_assigned_tasks,
)
from .auth_apikeys import revoke_all_other_sessions  # noqa: E402,F401
