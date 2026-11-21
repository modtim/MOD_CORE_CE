"""Auth/session domain for the current user — bulk session revocation.

Gate 2b migration (api-refactor opsx): the query moved to
`crud/me_auth_apikeys.py`, the revoke-all-but-current policy to
`services/me_auth_apikeys_service.py`. This handler is a thin delegation.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

import models, schemas
from dependencies import get_db, require_active_and_onboarded_user
from services import me_auth_apikeys_service

router = APIRouter()


@router.delete("/sessions", status_code=status.HTTP_200_OK)
def revoke_all_other_sessions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Revoke all sessions except the current one (most recent)."""
    return me_auth_apikeys_service.revoke_all_other_sessions(db, current_user)


@router.post("/signout-everywhere", status_code=status.HTTP_200_OK,
             response_model=schemas.UserSignOutEverywhereResult)
def sign_out_everywhere(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Sign the caller out EVERYWHERE (FX.112.p4b).

    Revokes every one of the caller's session records (including the
    current one) and all of their Keycloak sessions. This request
    completes normally, but the response says the client must re-login:
    the next request with the old token is dead. Audited
    (SESSION_SIGN_OUT_ALL)."""
    return me_auth_apikeys_service.sign_out_everywhere(db, current_user)
