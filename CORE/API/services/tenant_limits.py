"""MOD Community Edition — single-tenant guard.

Community Edition is a single-tenant install: once the one tenant exists, no
creation path may add another. This is a structural property of the edition, not
a licence check — there are no entitlements, tiers or limits behind it. Replaces
the full module in the Community Edition source tree and keeps its public
surface.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

import models
from services._base import ServiceError

logger = logging.getLogger(__name__)


class TenantLimitError(ServiceError):
    """Tenant creation refused: this edition holds exactly one tenant."""

    def __init__(self, message: str = "This edition supports a single tenant."):
        super().__init__(message, code="conflict")


def count_live_tenants(db: Session) -> int:
    return int(
        db.query(models.Tenant).filter(models.Tenant.deleted_at.is_(None)).count()
    )


def assert_can_create_tenant(db: Session) -> None:
    """Raise `TenantLimitError` once a live tenant already exists. The first
    tenant is always creatable. Fails closed if the count cannot be read."""
    try:
        count = db.query(models.Tenant).filter(models.Tenant.deleted_at.is_(None)).count()
    except Exception:
        logger.exception("single-tenant guard: could not read the tenants table; refusing")
        raise TenantLimitError(
            "Tenant creation is unavailable because the existing tenant count "
            "could not be determined. Please try again."
        )
    if not isinstance(count, int):
        return
    if count == 0:
        return
    raise TenantLimitError("This edition supports a single tenant.")
