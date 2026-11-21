"""Webhook CRUD operations - Webhook management within tenants."""
from typing import Optional, List

from fastapi import HTTPException
from sqlalchemy.orm import Session
import models, schemas
import secrets


def _require_https_url(url: str) -> None:
    # SSRF/transport guard: webhook delivery (services/outbound_webhooks.py)
    # POSTs from the API container to a tenant-controlled URL, so only https
    # may be registered. Same error type (HTTPException) the webhooks router
    # already uses for its other failures.
    if not (url or "").startswith("https://"):
        raise HTTPException(status_code=400, detail="Webhook URL must use HTTPS")


def create_webhook_endpoint(
    db: Session,
    webhook: schemas.WebhookEndpointCreate,
    tenant_id: int
) -> models.WebhookEndpoint:
    """Create a new webhook endpoint for a tenant."""
    _require_https_url(webhook.url)
    # Generate a secure secret for signing outgoing webhooks
    signing_secret = f"whsec_{secrets.token_urlsafe(32)}"

    db_webhook = models.WebhookEndpoint(
        url=webhook.url,
        description=webhook.description,
        subscribed_events=webhook.subscribed_events,
        is_active=webhook.is_active,
        tenant_id=tenant_id,
        signing_secret=signing_secret,
    )
    db.add(db_webhook)
    db.commit()
    db.refresh(db_webhook)
    return db_webhook


def get_webhook_endpoints(
    db: Session,
    tenant_id: int,
    skip: int = 0,
    limit: int = 100
) -> List[models.WebhookEndpoint]:
    """Retrieve all webhook endpoints for a specific tenant."""
    return (
        db.query(models.WebhookEndpoint)
        .filter(models.WebhookEndpoint.tenant_id == tenant_id)
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_webhook_endpoint(
    db: Session,
    webhook_id: int,
    tenant_id: int
) -> Optional[models.WebhookEndpoint]:
    """Retrieve a single webhook endpoint by its ID, ensuring it belongs to the correct tenant."""
    return (
        db.query(models.WebhookEndpoint)
        .filter(models.WebhookEndpoint.id == webhook_id, models.WebhookEndpoint.tenant_id == tenant_id)
        .first()
    )


def delete_webhook_endpoint(db: Session, db_webhook: models.WebhookEndpoint):
    """Soft-delete a webhook endpoint.

    iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    iso-no-hard-delete). `WebhookEndpoint` already carries SoftDeleteMixin and
    is now in database.py's soft-delete listener, so the tombstoned endpoint
    leaves every live read. Also flips `is_active` so delivery stops.
    """
    from datetime import datetime, timezone
    db_webhook.deleted_at = datetime.now(timezone.utc)
    db_webhook.is_active = False
    db.commit()
