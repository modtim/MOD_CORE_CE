"""POST /v1/stream-tickets — WS5.34b.

Mints a short-lived, single-use ticket an `EventSource` can pass via
`?ticket=` instead of the full Keycloak JWT (`?access_token=`), which used to
land in proxy access logs. Normal Bearer auth (same dependency every other
user endpoint uses); the ticket itself is opaque and means nothing outside
this process (see services/stream_tickets.py).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services.stream_tickets import TICKET_TTL_SECONDS, mint_ticket

logger = logging.getLogger(__name__)

router = APIRouter()


class StreamTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str
    run_id: str | None = None


class StreamTicketResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    ticket: str
    expires_in: int


@router.post("", response_model=StreamTicketResponse)
@router.post("/", response_model=StreamTicketResponse)
def create_stream_ticket(
    body: StreamTicketRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Mint a ticket for one of:
      - `{"scope": "notifications"}`
      - `{"scope": "wizard-run", "run_id": "<id>"}`
      - `{"scope": "workflow-debug", "run_id": "<id>"}`

    For `wizard-run`, access to that run is checked NOW (mint time) via the
    same `_require_access` the wizard-queue router uses elsewhere, using a
    404 (not 403) on no-access so existence doesn't leak across tenants —
    the SSE endpoint re-checks access again at redeem time.
    """
    if body.scope == "notifications":
        run_id = None
    elif body.scope == "wizard-run":
        if not body.run_id:
            raise HTTPException(status_code=422, detail="run_id required for wizard-run scope")
        # Local imports: avoid a hard import-time dependency from this small
        # router onto wizard_queue's full router module.
        from routers.wizard_queue import _get_run_or_404, _require_access
        run = _get_run_or_404(db, body.run_id)
        _require_access(current_user, run)
        run_id = body.run_id
    elif body.scope == "workflow-debug":
        if not body.run_id:
            raise HTTPException(
                status_code=422, detail="run_id required for workflow-debug scope"
            )
        # Local imports: same reason as the wizard-run branch above — keep this
        # small router from hard-depending on the debug-WS / workflows-shared
        # modules at import time.
        from crud import get_workflow_run_by_id
        from routers.workflows_debug_ws import RUN_NOT_FOUND
        from routers.workflows_domains._shared import _user_can_view_run

        try:
            run_id_int = int(body.run_id)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="run_id required for workflow-debug scope"
            )
        run = get_workflow_run_by_id(db, run_id=run_id_int, tenant_id=current_user.tenant_id)
        if not run or not _user_can_view_run(db, current_user, run):
            # 404 (not 403) so existence doesn't leak: unknown, foreign-tenant
            # and not-permitted runs get the exact same body — no oracle, and
            # the same reason string the debug WS closes 4403 with.
            raise HTTPException(status_code=404, detail=RUN_NOT_FOUND)
        run_id = str(run.id)
    else:
        raise HTTPException(status_code=422, detail="invalid scope")

    ticket = mint_ticket(
        user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        scope=body.scope,
        run_id=run_id,
    )
    return StreamTicketResponse(ticket=ticket, expires_in=TICKET_TTL_SECONDS)
