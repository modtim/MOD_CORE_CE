from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

import crud, models, schemas
from dependencies import get_db, require_active_and_onboarded_user

router = APIRouter(
    tags=["Webhooks (User-Managed)"],
    dependencies=[Depends(require_active_and_onboarded_user)],
    responses={404: {"description": "Not found"}},
)


@router.post("/", response_model=schemas.WebhookEndpointCreated, status_code=status.HTTP_201_CREATED)
def create_webhook_endpoint(
    webhook: schemas.WebhookEndpointCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Create a new webhook endpoint for the current user's tenant.
    A unique signing secret is generated automatically for verifying payloads.
    """
    # GSEC FX.161.p1b — refuse (403) a destination outside the tenant's
    # output allowlist (no-op when the tenant hasn't enforced one).
    # FX.162.p5 — CE carve: no DLP output allowlist -> no restriction.
    from utils.paid_import import optional_paid
    _dlp = optional_paid("services.dlp_output_allowlist")
    if _dlp is not None:
        _dlp.check_destination(
            db, current_user.tenant_id, webhook.url,
            kind="outbound_webhook", user_id=current_user.id,
        )
    # Note: The actual crud.create_webhook_endpoint function should handle
    # the generation of the signing_secret internally.
    db_webhook = crud.create_webhook_endpoint(db=db, webhook=webhook, tenant_id=current_user.tenant_id)
    if db_webhook is None:
        raise HTTPException(status_code=500, detail="Webhook creation failed")
    # BUG.B36b — the Dashboard's one-time key modal reads `created.secret`
    # (js/apps.js), while the ORM column is `signing_secret`; from_attributes
    # would not map those names, so the create response maps it explicitly.
    # The secret is shown ONCE here — list/get models never carry it.
    return schemas.WebhookEndpointCreated(
        id=db_webhook.id,
        tenant_id=db_webhook.tenant_id,
        url=db_webhook.url,
        description=db_webhook.description,
        subscribed_events=db_webhook.subscribed_events,
        is_active=db_webhook.is_active,
        created_at=db_webhook.created_at,
        secret=db_webhook.signing_secret,
    )


@router.get("/", response_model=List[schemas.WebhookEndpoint])
def read_webhook_endpoints(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Retrieve all webhook endpoints for the current user's tenant.
    """
    return crud.get_webhook_endpoints(db, tenant_id=current_user.tenant_id, skip=skip, limit=limit)


@router.get("/{webhook_id}", response_model=schemas.WebhookEndpoint)
def read_webhook_endpoint(
    webhook_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Retrieve a specific webhook endpoint from the current user's tenant.
    """
    db_webhook = crud.get_webhook_endpoint(db, webhook_id=webhook_id, tenant_id=current_user.tenant_id)
    if db_webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return db_webhook


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook_endpoint(
    webhook_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Delete a webhook endpoint from the current user's tenant.
    """
    db_webhook = crud.get_webhook_endpoint(db, webhook_id=webhook_id, tenant_id=current_user.tenant_id)
    if db_webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    crud.delete_webhook_endpoint(db=db, db_webhook=db_webhook)
    return

