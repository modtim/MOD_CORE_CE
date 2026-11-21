# API/routers/admin_resource_monitor.py
"""tenant-resource-monitor — platform-admin observability endpoints.

Exposes per-tenant + per-user resource consumption (native units:
GPU-seconds, CPU-core-seconds, RAM GB-seconds, IO MB) alongside
credits billed in the same window. No dollarization: operators see
*which* resource is being abused, not a rolled-up dollar number.

Every endpoint is gated by ``require_platform_admin``;
non-platform-admin callers get 403 + a
``ADMIN_CROSS_TENANT_DATA_ACCESS_BLOCKED`` audit row.

Spec: ``the internal design notes``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

import audit
import models
import schemas
import services.admin_resource_monitor_service as svc
from dependencies import get_db, require_active_and_onboarded_user
from services._base import ServiceError

logger = logging.getLogger(__name__)

# clean-v2-api (admin dissolve wave): moved from `/admin/resource-monitor/*`
# to `/system/resource-monitor/*`. `router` itself carries no prefix — its
# routes live on `_impl`, mounted at the canonical prefix below the endpoint
# defs. The old `/admin/resource-monitor/settings` back-compat alias for
# CORE/Dashboard/js/settings.js has been removed (final clean-cut) now that
# settings.js is repointed to `/system/resource-monitor/settings`.
router = APIRouter(tags=["System - Resource Monitor"])
_impl = APIRouter()


def require_platform_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
    request: Request = None,
) -> models.User:
    if not getattr(current_user, "is_platform_admin", False):
        try:
            audit.log_cross_tenant_data_access_blocked(
                db=db,
                admin_user_id=current_user.id,
                target_tenant_id=current_user.tenant_id,
                resource_type="resource_monitor",
                resource_id=0,
                request=request,
            )
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform admin only",
        )
    # open-core-tier-structure: self-host fleet resource dashboards are a gated
    # feature (the platform admin is the customer's own ops team); gate behind
    # FLEET_CONSOLE. On SaaS this is MOD's internal ops — platform-admin gate above
    # is the control, no cap gate. CE edition bypasses the cap; Pro/self-host
    # without the entitlement still gets 402 fleet_console_required (tests pin it).
    from utils.entitlements import is_saas, get_entitlements, evaluate_feature, Cap
    from utils.paid_import import is_ce_edition
    if not is_saas() and not is_ce_edition():
        if not evaluate_feature(Cap.FLEET_CONSOLE, get_entitlements(db)).allowed:
            raise HTTPException(status_code=402, detail={"code": "fleet_console_required"})
    return current_user


# =============================================================================
# Helpers
# =============================================================================


_ERROR_STATUS = {"not_found": 404, "conflict": 409, "bad_request": 400}


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer `ServiceError` to the equivalent `HTTPException`.
    Preserves this router's pre-migration status codes/detail shapes."""
    raise HTTPException(
        status_code=_ERROR_STATUS.get(exc.code, 400),
        detail=exc.message,
    )


# =============================================================================
# CE-safe lockbox gate
# =============================================================================
# In CE the services.lockbox module is absent. We must never fail-open:
# absent lockbox means deny cross-tenant access, pass only own-tenant calls.
# Mirrors the existing lockbox.py gate shape (403 + {"code":"lockbox_required"}).
# =============================================================================
from utils.paid_import import optional_paid


def _require_lockbox(
    db: Session, admin: models.User, tenant_id: int,
    write: bool = False, request: Request = None,
) -> None:
    """Gate for staff cross-tenant content access — CE-safe.

    Tries to import services.lockbox. If present, delegates to
    require_lockbox_for_cross_tenant (the same gate). If absent (CE carve),
    applies the fail-closed rule: only pass when the target is the caller's
    own tenant or tenant_id == 0 (system sentinel).
    """
    lockbox = optional_paid("services.lockbox")
    if lockbox is not None:
        lockbox.require_lockbox_for_cross_tenant(
            db, admin, tenant_id, write=write, request=request)
        return
    # CE carve: lockbox absent → deny cross-tenant, allow own-tenant
    own = getattr(admin, "tenant_id", None)
    if int(tenant_id or 0) == 0 or tenant_id == own:
        return
    # Same audit event lockbox.py writes for a denied attempt (LOCKBOX_ACCESS,
    # transition=denied) — the carve must not lose the audit trail.
    try:
        audit.log_security_event(
            db=db, action=models.AuditAction.LOCKBOX_ACCESS, request=request,
            user_id=admin.id, tenant_id=tenant_id,
            metadata={
                "transition": "denied",
                "target_tenant_id": tenant_id,
                "staff_user_id": admin.id,
                "need_write": bool(write),
                "lockbox_request_id": None,
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("RESMON-CE lockbox-denial audit write failed: %s", exc)
    raise HTTPException(
        status_code=403,
        detail={"code": "lockbox_required", "tenant_id": tenant_id},
        headers={"X-Error-Code": "LOCKBOX_REQUIRED"},
    )


# =============================================================================
# Endpoints
# =============================================================================


@_impl.get("/tenants", response_model=List[schemas.ResourceMonitorTenantRow])
def list_tenants(
    window: str = Query("24h", pattern="^(1h|24h|7d|30d)$"),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(require_platform_admin),
):
    """Per-tenant native-unit rollup in the selected window."""
    return svc.list_tenants(db, window)


@_impl.get("/tenants/{tenant_id}/users", response_model=List[schemas.ResourceMonitorUserRow])
def list_users_in_tenant(
    tenant_id: int,
    window: str = Query("24h", pattern="^(1h|24h|7d|30d)$"),
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_platform_admin),
    request: Request = None,
):
    """Per-user breakdown inside a single tenant."""
    # FX.67 (Q50=A) — staff cross-tenant content access needs an approved
    # lockbox grant (part 1: the most direct per-tenant drill-downs). CE-safe.
    _require_lockbox(db, admin, tenant_id, write=False, request=request)
    return svc.list_users_in_tenant(db, tenant_id, window, admin_id=admin.id, request=request)


@_impl.get(
    "/tenants/{tenant_id}/users/{user_id}/tasks",
    response_model=List[schemas.ResourceMonitorTaskRow],
)
def list_tasks_for_user(
    tenant_id: int,
    user_id: int,
    window: str = Query("24h", pattern="^(1h|24h|7d|30d)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_platform_admin),
    request: Request = None,
):
    """Per-task list inside (tenant, user), sorted by GPU-sec desc.

    user_id == 0 means the standalone-task bucket (tasks not attached
    to any workflow run — calibration, benchmarks, etc.).
    """
    # FX.67 (Q50=A) — per-task drill-down is the most granular per-tenant
    # content read; needs an approved lockbox grant (part 1). CE-safe.
    _require_lockbox(db, admin, tenant_id, write=False, request=request)
    return svc.list_tasks_for_user(
        db, tenant_id, user_id, window,
        admin_id=admin.id, request=request, limit=limit, offset=offset,
    )


@_impl.get("/settings", response_model=schemas.ResourceMonitorConfigRead)
def get_settings(
    db: Session = Depends(get_db),
    _admin: models.User = Depends(require_platform_admin),
):
    """Read the singleton resource_monitor_config row."""
    return svc.get_settings(db)


@_impl.put("/settings", response_model=schemas.ResourceMonitorConfigRead)
def update_settings(
    payload: schemas.ResourceMonitorConfigUpdate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_platform_admin),
    request: Request = None,
):
    """Apply partial updates to the singleton config row."""
    return svc.update_settings(
        db,
        admin_id=admin.id,
        values=payload.model_dump(exclude_unset=True),
        request=request,
    )


@_impl.put("/tenants/{tenant_id}/threshold", response_model=Dict[str, Any])
def set_tenant_threshold(
    tenant_id: int,
    payload: schemas.TenantThresholdOverride,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_platform_admin),
    request: Request = None,
):
    """Write a per-tenant override of any subset of the four per-resource
    thresholds. Omitted keys fall back to the platform default at scan
    time."""
    try:
        return svc.set_tenant_threshold(
            db,
            tenant_id=tenant_id,
            admin_id=admin.id,
            override=payload.model_dump(exclude_unset=True),
            request=request,
        )
    except ServiceError as exc:
        _raise_http(exc)


router.include_router(_impl, prefix="/system/resource-monitor")
