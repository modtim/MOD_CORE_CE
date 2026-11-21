from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dependencies import get_db, require_service_account_token

# This router is for internal, service-to-service communication.
router = APIRouter(
    tags=["Private Services"],
    # Uses Keycloak service account authentication (client credentials flow)
    dependencies=[Depends(require_service_account_token)],
    responses={404: {"description": "Not found"}},
)

# GET /private/invitations/{invitation_id}/details lives in
# routers.invitations (``get_invitation_details_by_id``) — it was duplicated
# here and shadowed (dead code), so it was removed (WS1.40d).

