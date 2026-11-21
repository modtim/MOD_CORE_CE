from typing import List
from fastapi import APIRouter, Depends, status, BackgroundTasks

from sqlalchemy.orm import Session

import schemas
import models
import services.invitations_service as invitations_service
from dependencies import get_db, require_active_onboarded_parent_user, require_service_account_token, require_capability

# This router is for actions that require an authenticated, authorized user.
# Specifically, only the tenant's parent user can manage invitations.
#
# clean-v2-api identity wave: prefix moved from bare `/invitations` (added by
# main.py's router_mapping tuple) to `/users/invitations` (design.md — "users
# (+ invitations as users/invitations)"). The prefix now lives on the router
# itself; main.py's tuple must change its extra-prefix string from
# "/invitations" to "" (see the identity-wave report for the exact line).
router = APIRouter(
    prefix="/users/invitations",
    tags=["Invitations"],
    dependencies=[Depends(require_active_onboarded_parent_user)],
)

# This router is for public-facing actions, like checking a token's validity.
public_router = APIRouter(
    prefix="/public/invitations",
    tags=["Invitations (Public)"]
)

# This router is for service-to-service actions (service-account tokens).
private_router = APIRouter(
    prefix="/private/invitations",
    tags=["Invitations (Private)"]
)

@router.post("/", response_model=schemas.InvitationCreated, status_code=status.HTTP_201_CREATED)
def create_and_send_invitation(
        invitation_data: schemas.InvitationCreate,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_capability(models.ResourceType.INVITATION, "create")),
):
    """
    Creates an invitation for a new user to join the current user's tenant
    and sends the invitation email via the configured mailer.

    - **Security**: Only the active, onboarded parent user of a tenant can call this.
    - **Platform Admin**: Can specify tenant_id to invite users to any tenant.
    """
    return invitations_service.create_and_send_invitation(
        db,
        invitation_data,
        background_tasks,
        current_user,
    )


@router.get("/", response_model=List[schemas.Invitation])
def list_pending_invitations_for_tenant(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_capability(models.ResourceType.INVITATION, "view")),
        tenant_id: int = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "expires_at",
        order: str = "asc"
):
    """
    Lists all pending invitations for the current user's tenant with pagination and sorting.

    Platform admins can optionally specify tenant_id to view invitations for any tenant.
    """
    return invitations_service.list_pending_invitations(
        db,
        current_user,
        tenant_id=tenant_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        order=order,
    )


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_invitation(
        invitation_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_capability(models.ResourceType.INVITATION, "cancel")),
):
    """
    Cancels a pending invitation, preventing it from being used.

    Platform admins can cancel invitations from any tenant.
    """
    invitations_service.cancel_invitation(db, current_user, invitation_id)

    # Optional: You could trigger another background task here to notify the
    # user that their invitation was canceled if you wish.
    # background_tasks.add_task(...)

    return


# --- Private Endpoints (Service-to-Service) ---

@private_router.get("/{invitation_id}/details")
def get_invitation_details_by_id(
        invitation_id: int,
        db: Session = Depends(get_db),
        service_claims: dict = Depends(require_service_account_token)
):
    """
    Private endpoint for service-account callers to fetch invitation details by ID.
    This is used by the send_invitation_email_dag to get all necessary information
    to send the invitation email.

    Requires a valid service account token from Keycloak (client credentials flow).

    Returns data in the format expected by the worker:
    - recipient_email: Email address of the invitee
    - recipient_name: Name extracted from email (before @)
    - invitation_token: Unique token for the invitation link
    """
    return invitations_service.get_invitation_details(db, invitation_id)


# --- Public Endpoints ---

@public_router.get("/{token}", response_model=schemas.Invitation)
def get_invitation_details_by_token(token: str, db: Session = Depends(get_db)):
    """
    A public endpoint for the frontend to verify an invitation token.
    This allows the UI to show details like "You have been invited by..."
    before the user signs up.
    """
    return invitations_service.get_invitation_by_token(db, token)