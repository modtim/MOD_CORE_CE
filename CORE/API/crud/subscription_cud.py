"""Subscription CRUD operations - Subscription management within tenants."""
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
import models
from datetime import datetime, timedelta


def get_subscription_for_tenant(
    db: Session,
    tenant_id: int
) -> Optional[models.Subscription]:
    """Retrieve the subscription for a specific tenant, eagerly loading the related plan details."""
    return (
        db.query(models.Subscription)
        .options(joinedload(models.Subscription.plan))
        .filter(models.Subscription.tenant_id == tenant_id)
        .first()
    )


def update_subscription_after_payment(
    db: Session,
    stripe_subscription_id: str,
    new_period_end: datetime
) -> Optional[models.Subscription]:
    """Find a subscription by its checkout ID and update its period end date and status."""
    db_subscription = db.query(models.Subscription).filter(
        models.Subscription.stripe_subscription_id == stripe_subscription_id).first()

    if db_subscription:
        db_subscription.current_period_ends_at = new_period_end
        db_subscription.status = "active"  # Ensure the status is active
        db.commit()
        db.refresh(db_subscription)

    return db_subscription


def update_subscription_status(
    db: Session,
    stripe_subscription_id: str,
    new_status: str
) -> Optional[models.Subscription]:
    """Find a subscription by its checkout ID and update its status."""
    db_subscription = db.query(models.Subscription).filter(
        models.Subscription.stripe_subscription_id == stripe_subscription_id).first()

    if db_subscription:
        db_subscription.status = new_status
        db.commit()
        db.refresh(db_subscription)

    return db_subscription
