"""Admin Resource Monitor CRUD — the ONLY place `db.query(...)` lives for the
tenant-resource-monitor admin-observability domain
(`routers/admin_resource_monitor.py`).

These are cross-tenant, platform-admin aggregations (per-tenant / per-user /
per-task rollups) plus the singleton config row and per-tenant threshold
overrides. Access control (platform-admin gate, entitlement cap) lives in the
router's `require_platform_admin` dependency, not here — every function below
is a plain, tenant-parameterised query or write primitive with no policy.

Bounded lists (`task_aggregates_for_scope`) take an explicit `limit`/`offset`;
none of the queries here are unbounded.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func as sql_func
from sqlalchemy.orm import Session

import models


# =============================================================================
# Per-tenant rollup
# =============================================================================


def resource_rollup_by_tenant(db: Session, since: datetime, until: datetime) -> List[Any]:
    """Sum of native-unit resource usage per tenant in `[since, until)`."""
    return db.query(
        models.TaskResourceAggregate.tenant_id,
        sql_func.sum(models.TaskResourceAggregate.gpu_seconds).label("gpu_seconds"),
        sql_func.sum(models.TaskResourceAggregate.cpu_core_seconds).label("cpu_core_seconds"),
        sql_func.sum(models.TaskResourceAggregate.ram_gb_seconds).label("ram_gb_seconds"),
        sql_func.sum(
            models.TaskResourceAggregate.disk_read_mb
            + models.TaskResourceAggregate.disk_write_mb
        ).label("io_mb"),
        sql_func.count(models.TaskResourceAggregate.id).label("task_count"),
    ).filter(
        models.TaskResourceAggregate.completed_at >= since,
        models.TaskResourceAggregate.completed_at < until,
    ).group_by(models.TaskResourceAggregate.tenant_id).all()


def credits_by_tenant(db: Session, since: datetime, until: datetime) -> List[Any]:
    """Rows of `(tenant_id, credits_used)` summed in `[since, until)`."""
    # Guard: CE has no CreditUsageLog model - keys on model presence only; no licence gate and no quantity cap.
    if getattr(models, "CreditUsageLog", None) is None:
        return []
    return db.query(
        models.CreditUsageLog.tenant_id,
        sql_func.sum(models.CreditUsageLog.credits_used).label("credits_used"),
    ).filter(
        models.CreditUsageLog.created_at >= since,
        models.CreditUsageLog.created_at < until,
    ).group_by(models.CreditUsageLog.tenant_id).all()


def tenants_by_ids(db: Session, tenant_ids: Sequence[int]) -> Dict[int, models.Tenant]:
    if not tenant_ids:
        return {}
    return {
        t.id: t
        for t in db.query(models.Tenant).filter(models.Tenant.id.in_(tenant_ids)).all()
    }


def active_plan_names_by_tenant(db: Session, tenant_ids: Sequence[int]) -> Dict[int, Optional[str]]:
    """`{tenant_id: plan_name}` for tenants with an active subscription."""
    if not tenant_ids:
        return {}
    rows = db.query(
        models.Subscription.tenant_id,
        models.Plan.name,
    ).join(models.Plan, models.Plan.id == models.Subscription.plan_id).filter(
        models.Subscription.tenant_id.in_(tenant_ids),
        models.Subscription.status == "active",
    ).all()
    return {row.tenant_id: row.name for row in rows}


# =============================================================================
# Per-user rollup (within a tenant)
# =============================================================================


def resource_rollup_by_user(
    db: Session, tenant_id: int, since: datetime, until: datetime
) -> List[Any]:
    return db.query(
        models.TaskResourceAggregate.user_id,
        sql_func.sum(models.TaskResourceAggregate.gpu_seconds).label("gpu_seconds"),
        sql_func.sum(models.TaskResourceAggregate.cpu_core_seconds).label("cpu_core_seconds"),
        sql_func.sum(models.TaskResourceAggregate.ram_gb_seconds).label("ram_gb_seconds"),
        sql_func.sum(
            models.TaskResourceAggregate.disk_read_mb
            + models.TaskResourceAggregate.disk_write_mb
        ).label("io_mb"),
        sql_func.count(models.TaskResourceAggregate.id).label("task_count"),
    ).filter(
        models.TaskResourceAggregate.tenant_id == tenant_id,
        models.TaskResourceAggregate.completed_at >= since,
        models.TaskResourceAggregate.completed_at < until,
    ).group_by(models.TaskResourceAggregate.user_id).all()


def credits_for_tenant(db: Session, tenant_id: int, since: datetime, until: datetime) -> int:
    # Guard: CE has no CreditUsageLog model - keys on model presence only; no licence gate and no quantity cap.
    if getattr(models, "CreditUsageLog", None) is None:
        return 0
    row = db.query(
        sql_func.sum(models.CreditUsageLog.credits_used).label("credits_used"),
    ).filter(
        models.CreditUsageLog.tenant_id == tenant_id,
        models.CreditUsageLog.created_at >= since,
        models.CreditUsageLog.created_at < until,
    ).one_or_none()
    return int(row.credits_used or 0) if row else 0


def users_by_ids(db: Session, user_ids: Sequence[int]) -> Dict[int, models.User]:
    if not user_ids:
        return {}
    return {
        u.id: u
        for u in db.query(models.User).filter(models.User.id.in_(user_ids)).all()
    }


# =============================================================================
# Per-task list (bounded)
# =============================================================================


def task_aggregates_for_scope(
    db: Session,
    tenant_id: int,
    user_id: Optional[int],
    since: datetime,
    until: datetime,
    *,
    limit: int,
    offset: int,
) -> List[models.TaskResourceAggregate]:
    """Bounded, GPU-seconds-desc slice of `TaskResourceAggregate` rows for
    (tenant_id, user_id) in `[since, until)`. `user_id=None` selects the
    standalone-task bucket (`user_id IS NULL`)."""
    q = db.query(models.TaskResourceAggregate).filter(
        models.TaskResourceAggregate.tenant_id == tenant_id,
        models.TaskResourceAggregate.completed_at >= since,
        models.TaskResourceAggregate.completed_at < until,
    )
    if user_id is None:
        q = q.filter(models.TaskResourceAggregate.user_id.is_(None))
    else:
        q = q.filter(models.TaskResourceAggregate.user_id == user_id)
    q = q.order_by(models.TaskResourceAggregate.gpu_seconds.desc())
    return q.offset(offset).limit(limit).all()


def get_task(db: Session, task_id: int) -> Optional[models.ApiTaskQueue]:
    return db.query(models.ApiTaskQueue).filter(models.ApiTaskQueue.id == task_id).one_or_none()


# =============================================================================
# Singleton config row
# =============================================================================


def get_config(db: Session) -> Optional[models.ResourceMonitorConfig]:
    return db.query(models.ResourceMonitorConfig).filter(
        models.ResourceMonitorConfig.singleton_id == 1,
    ).one_or_none()


def create_default_config(db: Session, *, commit: bool = True) -> models.ResourceMonitorConfig:
    row = models.ResourceMonitorConfig(singleton_id=1)
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def apply_config_updates(row: models.ResourceMonitorConfig, values: Dict[str, Any]) -> Dict[str, Any]:
    """Apply `values` onto `row`, returning a `{field: {old, new}}` diff for
    changed fields only. Does NOT commit — caller owns the transaction."""
    changes: Dict[str, Any] = {}
    for field, value in values.items():
        old = getattr(row, field)
        if old != value:
            changes[field] = {"old": old, "new": value}
            setattr(row, field, value)
    return changes


# =============================================================================
# Per-tenant threshold override
# =============================================================================


def get_tenant(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).one_or_none()


def get_tenant_setting(
    db: Session, tenant_id: int, key: str
) -> Optional[models.TenantSetting]:
    return db.query(models.TenantSetting).filter(
        models.TenantSetting.tenant_id == tenant_id,
        models.TenantSetting.key == key,
    ).one_or_none()


def create_tenant_setting(
    db: Session, tenant_id: int, key: str, value: Dict[str, Any]
) -> models.TenantSetting:
    """Insert a new `TenantSetting` row. Does NOT commit."""
    row = models.TenantSetting(tenant_id=tenant_id, key=key, value=value)
    db.add(row)
    return row
