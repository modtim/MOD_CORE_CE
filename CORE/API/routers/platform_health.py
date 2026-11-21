"""Platform Health Check Router

Gate 1 health merge: a single `/health` surface replaces the former
three-endpoint split (`/health/status`, `/platform/health/detailed`,
`/platform/health/extended`). Detail level is selected by query param:

- GET /health                — public status rollup (no auth). Same body
                                as the old `/health/status`.
- GET /health?verbose=true    — latency + per-component error detail. Same
                                body as the old `/platform/health/detailed`.
                                R-094: now requires an authenticated user
                                (raw error strings + latencies are internal
                                detail). Anonymous `verbose=true` falls back
                                to the plain public rollup — NOT an error, so
                                uptime probes keep working.
- GET /health?extended=true   — deep platform-admin snapshot (containers /
                                resources / events / TLS certs + Postmark /
                                Stripe freshness + DB pool + vault rotation).
                                Same body as the old
                                `/platform/health/extended`. Requires an
                                authenticated, active, onboarded platform
                                admin — non-admins/anonymous get 403.

`extended` wins over `verbose` if both are set. The probe logic itself
(services/platform_health_service.py, services/platform_health_extended_service.py)
is unchanged — only the routing/paths were consolidated.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

import models
import services.platform_health_service as platform_health_service
# Extended (platform-admin analytics) health is a Paid module: present in the
# full build, absent in CE. The basic /health path below never touches it.
try:
    import services.platform_health_extended_service as platform_health_extended_service
except ImportError:  # CE build: Paid module absent
    platform_health_extended_service = None
from dependencies import get_db, get_optional_current_user
# Re-exported for backward-compat import paths (tests/test_status_page.py
# imports these directly from routers.platform_health; the implementation
# now lives in services/platform_health_service.py — Gate 2b migration).
from services.platform_health_service import (
    SERVICES,
    ComponentStatus,
    PublicStatusResponse,
    DetailedStatusResponse,
    _check_database,
    _check_keycloak,
    _check_minio,
    _check_redis,
    _check_harbor,
    _check_model_cache,
    _check_workers,
    _run_all_checks,
    _overall_status,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_platform_admin(current_user: Optional[models.User]) -> models.User:
    if (
        current_user is None
        or current_user.status != models.UserStatus.ACTIVE
        or not current_user.onboarding_complete
        or not current_user.is_platform_admin
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires platform admin privileges",
        )
    return current_user


@router.get("/health", include_in_schema=True)
async def health(
    verbose: bool = False,
    extended: bool = False,
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(get_optional_current_user),
):
    """Unified platform health endpoint. See module docstring for the
    `?verbose`/`?extended` detail levels."""
    if extended:
        _require_platform_admin(current_user)
        if platform_health_extended_service is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Extended platform health is not available in this edition",
            )
        snapshot = await platform_health_extended_service.get_extended_health_snapshot(db)
        return snapshot
    if verbose and current_user is not None:
        # R-094: detailed status carries raw per-component error strings and
        # latencies — internal detail, so it requires an authenticated user.
        detailed = await platform_health_service.get_detailed_status(db)
        payload = detailed.model_dump()
        return payload
    if verbose:
        # Anonymous verbose: degrade to the public rollup (uptime probes must
        # not break), never error. Kept at the historical three-key shape.
        logger.warning("/health?verbose=true requested anonymously — serving public rollup only")
        return await platform_health_service.get_public_status(db)
    # Plain anonymous rollup: unchanged historical three-key shape. The
    # tier-gate mode is an internal entitlement-enforcement detail, not
    # something to disclose to unauthenticated callers — see the
    # authenticated verbose/extended branches above.
    return await platform_health_service.get_public_status(db)
