"""Admin Resource Monitor Service — business logic/policy for the
tenant-resource-monitor platform-admin endpoints
(`routers/admin_resource_monitor.py`).

Owns: window-token -> time-bounds translation, native-unit-per-credit ratio
math, the per-tenant/per-user/per-task rollup shaping + "worst offender
first" sort, the settings get-or-create + audit-on-change policy, and the
per-tenant threshold-override merge policy. All data access goes through
`crud/admin_resource_monitor.py` — no `db.query` here.

Auth (platform-admin gate + the open-core FLEET_CONSOLE cap) stays in the
router's `require_platform_admin` dependency — this module assumes the
caller has already been authorized and only needs the resolved actor's id
for audit attribution.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import crud.admin_resource_monitor as crud
import models
from services._base import NotFoundError, ServiceError
from services.resource_monitor import window_seconds_for


class BadRequestError(ServiceError):
    """Malformed/invalid request payload (router maps -> 400)."""

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


# =============================================================================
# Window bounds + ratio math
# =============================================================================


def window_bounds(token: str) -> tuple[datetime, datetime]:
    """Translate a window token (``1h``/``24h``/``7d``/``30d``) into
    ``(since, until)`` UTC bounds ending now."""
    now = datetime.now(timezone.utc)
    return now - timedelta(seconds=window_seconds_for(token)), now


def ratio(numerator: float, credits: int) -> float:
    """Native-unit-per-credit ratio. +∞ sentinel (1e9) when credits=0 so the
    UI can render an ∞ pill rather than a blank cell."""
    if credits <= 0:
        return 1e9 if numerator > 0 else 0.0
    return numerator / credits


def _worst_ratio(row: Dict[str, Any]) -> float:
    return max(
        row["gpu_seconds_per_credit"],
        row["cpu_core_seconds_per_credit"],
        row["ram_gb_seconds_per_credit"],
        row["io_mb_per_credit"],
    )


# =============================================================================
# Endpoint orchestration — one function per router handler, so handlers stay
# a one-line call. Each wraps window-token resolution (+ audit, where the
# original handler emitted one) around the pure rollup builders below.
# =============================================================================


def list_tenants(db: Session, window: str) -> List[Dict[str, Any]]:
    """Per-tenant native-unit rollup in the selected window."""
    since, until = window_bounds(window)
    return tenant_rollup(db, since, until)


def list_users_in_tenant(
    db: Session, tenant_id: int, window: str, *, admin_id: int, request: Any = None,
) -> List[Dict[str, Any]]:
    """Per-user breakdown inside a single tenant. Audits the admin's view."""
    import audit

    audit.log_admin_tenant_view(
        db=db,
        admin_user_id=admin_id,
        target_tenant_id=tenant_id,
        request=request,
        metadata={"surface": "resource_monitor.users"},
    )
    since, until = window_bounds(window)
    return user_rollup(db, tenant_id, since, until)


def list_tasks_for_user(
    db: Session,
    tenant_id: int,
    user_id: int,
    window: str,
    *,
    admin_id: int,
    request: Any = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Per-task list inside (tenant, user), sorted by GPU-sec desc. Audits the
    admin's view. `user_id == 0` means the standalone-task bucket (tasks not
    attached to any workflow run — calibration, benchmarks, etc.)."""
    import audit

    audit.log_admin_tenant_view(
        db=db,
        admin_user_id=admin_id,
        target_tenant_id=tenant_id,
        request=request,
        metadata={"surface": "resource_monitor.tasks", "target_user_id": user_id},
    )
    since, until = window_bounds(window)
    resolved_uid: Optional[int] = None if user_id == 0 else user_id
    return task_list(db, tenant_id, resolved_uid, since, until, limit=limit, offset=offset)


# =============================================================================
# Per-tenant rollup
# =============================================================================


def tenant_rollup(db: Session, since: datetime, until: datetime) -> List[Dict[str, Any]]:
    """Per-tenant native-unit rollup, sorted worst-offender-first (the
    highest single per-resource ratio, regardless of which resource leaks)."""
    resource_rows = crud.resource_rollup_by_tenant(db, since, until)

    tenant_credits: Dict[int, int] = {
        row.tenant_id: int(row.credits_used or 0)
        for row in crud.credits_by_tenant(db, since, until)
    }

    tenant_ids = [r.tenant_id for r in resource_rows]
    tenants = crud.tenants_by_ids(db, tenant_ids)
    plan_by_tenant = crud.active_plan_names_by_tenant(db, tenant_ids)

    out: List[Dict[str, Any]] = []
    for row in resource_rows:
        tenant = tenants.get(row.tenant_id)
        gpu = float(row.gpu_seconds or 0.0)
        cpu = float(row.cpu_core_seconds or 0.0)
        ram = float(row.ram_gb_seconds or 0.0)
        io = float(row.io_mb or 0.0)
        credits = tenant_credits.get(row.tenant_id, 0)
        out.append({
            "tenant_id": row.tenant_id,
            "tenant_name": tenant.name if tenant else "(unknown)",
            "plan_name": plan_by_tenant.get(row.tenant_id),
            "gpu_seconds": gpu,
            "cpu_core_seconds": cpu,
            "ram_gb_seconds": ram,
            "io_mb": io,
            "credits_used": credits,
            "gpu_seconds_per_credit": ratio(gpu, credits),
            "cpu_core_seconds_per_credit": ratio(cpu, credits),
            "ram_gb_seconds_per_credit": ratio(ram, credits),
            "io_mb_per_credit": ratio(io, credits),
            "task_count": int(row.task_count or 0),
        })
    out.sort(key=_worst_ratio, reverse=True)
    return out


# =============================================================================
# Per-user rollup (within a tenant)
# =============================================================================


def user_rollup(
    db: Session, tenant_id: int, since: datetime, until: datetime
) -> List[Dict[str, Any]]:
    resource_rows = crud.resource_rollup_by_user(db, tenant_id, since, until)
    # Conservative attribution: tenant credit is attributed against each of
    # that tenant's users. Lowers ratios -> avoids false positives.
    tenant_credits = crud.credits_for_tenant(db, tenant_id, since, until)

    user_ids = [r.user_id for r in resource_rows if r.user_id is not None]
    users = crud.users_by_ids(db, user_ids)

    out: List[Dict[str, Any]] = []
    for row in resource_rows:
        gpu = float(row.gpu_seconds or 0.0)
        cpu = float(row.cpu_core_seconds or 0.0)
        ram = float(row.ram_gb_seconds or 0.0)
        io = float(row.io_mb or 0.0)
        credits = tenant_credits
        user = users.get(row.user_id) if row.user_id else None
        out.append({
            "user_id": row.user_id,
            "user_email": user.email if user else None,
            "gpu_seconds": gpu,
            "cpu_core_seconds": cpu,
            "ram_gb_seconds": ram,
            "io_mb": io,
            "credits_used": credits,
            "gpu_seconds_per_credit": ratio(gpu, credits),
            "cpu_core_seconds_per_credit": ratio(cpu, credits),
            "ram_gb_seconds_per_credit": ratio(ram, credits),
            "io_mb_per_credit": ratio(io, credits),
            "task_count": int(row.task_count or 0),
        })
    out.sort(key=_worst_ratio, reverse=True)
    return out


# =============================================================================
# Per-task list (bounded)
# =============================================================================


def task_list(
    db: Session,
    tenant_id: int,
    user_id: Optional[int],
    since: datetime,
    until: datetime,
    *,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Per-task list inside (tenant, user), sorted by GPU-sec desc.

    `user_id=None` selects the standalone-task bucket (tasks not attached to
    any workflow run — calibration, benchmarks, etc.).
    """
    rows = crud.task_aggregates_for_scope(
        db, tenant_id, user_id, since, until, limit=limit, offset=offset,
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        task = crud.get_task(db, row.task_id)
        out.append({
            "task_id": row.task_id,
            "node_id": task.node_id if task else None,
            "workflow_run_id": task.workflow_run_id if task else None,
            "gpu_seconds": row.gpu_seconds,
            "cpu_core_seconds": row.cpu_core_seconds,
            "ram_gb_seconds": row.ram_gb_seconds,
            "io_mb": row.disk_read_mb + row.disk_write_mb,
            "completed_at": row.completed_at,
        })
    return out


# =============================================================================
# Singleton config row
# =============================================================================


def get_settings(db: Session) -> models.ResourceMonitorConfig:
    """Read the singleton `resource_monitor_config` row, creating it with
    defaults on first access."""
    row = crud.get_config(db)
    if row is None:
        row = crud.create_default_config(db)
    return row


def update_settings(
    db: Session,
    *,
    admin_id: int,
    values: Dict[str, Any],
    request: Any = None,
) -> models.ResourceMonitorConfig:
    """Apply a partial update to the singleton config row, auditing only the
    fields that actually changed."""
    import audit

    row = crud.get_config(db)
    if row is None:
        row = crud.create_default_config(db, commit=False)

    changes = crud.apply_config_updates(row, values)
    row.updated_by_user_id = admin_id

    if changes:
        log = audit.log_admin_resource_monitor_settings_updated(
            db=db, admin_user_id=admin_id, changes=changes, request=request,
        )
        if log is not None:
            row.audit_event_id = log.id
        db.commit()
        db.refresh(row)
    return row


# =============================================================================
# Per-tenant threshold override
# =============================================================================


def set_tenant_threshold(
    db: Session,
    *,
    tenant_id: int,
    admin_id: int,
    override: Dict[str, Any],
    request: Any = None,
) -> Dict[str, Any]:
    """Write a per-tenant override of any subset of the four per-resource
    thresholds. Omitted keys fall back to the platform default at scan time.

    Raises `NotFoundError` if the tenant doesn't exist and `BadRequestError`
    if `override` is empty.
    """
    import audit

    tenant = crud.get_tenant(db, tenant_id)
    if tenant is None:
        raise NotFoundError("Tenant not found")

    if not override:
        raise BadRequestError("At least one threshold required")

    setting = crud.get_tenant_setting(db, tenant_id, "resource_leak_threshold")
    old = setting.value if setting else None

    if setting is None:
        setting = crud.create_tenant_setting(
            db, tenant_id, "resource_leak_threshold", override,
        )
    else:
        # Merge: keep prior keys, override with new ones.
        merged = dict(old or {}) if isinstance(old, dict) else {}
        merged.update(override)
        setting.value = merged

    audit.log_admin_resource_monitor_settings_updated(
        db=db,
        admin_user_id=admin_id,
        changes={
            "tenant_id": tenant_id,
            "resource_leak_threshold": {"old": old, "new": setting.value},
        },
        request=request,
    )
    db.commit()
    return {"tenant_id": tenant_id, "resource_leak_threshold": setting.value}
