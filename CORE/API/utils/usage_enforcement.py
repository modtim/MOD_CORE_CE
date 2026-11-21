"""MOD Community Edition — usage-enforcement stub.

Community Edition has no seat, storage, concurrency or spend limits, so every
check here allows. The in-flight run counters are plain bookkeeping and keep
working. Replaces the full module in the Community Edition source tree and keeps
its public surface so shipped callers work unchanged.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

_OK: Tuple[bool, Optional[str]] = (True, None)


def _is_saas() -> bool:
    return False


class UsageLimitError(Exception):
    def __init__(self, limit_type: str, message: str, upgrade_suggestion: str = None):
        self.limit_type = limit_type
        self.message = message
        self.upgrade_suggestion = upgrade_suggestion
        super().__init__(message)


def check_subscription_status(db: Session, tenant_id: int, raise_on_suspended: bool = True) -> Tuple[bool, Optional[str]]:
    return _OK


def check_seat_limit(db: Session, tenant_id: int, raise_on_limit: bool = True) -> Tuple[bool, Optional[str]]:
    return _OK


def check_storage_limit(db: Session, tenant_id: int, additional_bytes: int,
                        raise_on_limit: bool = True) -> Tuple[bool, Optional[str]]:
    return _OK


def check_concurrent_process_limit(db: Session, tenant_id: int, raise_on_limit: bool = False,
                                   exclude_run_id: Optional[int] = None) -> Tuple[bool, Optional[str]]:
    return _OK


def check_overage_cap(db: Session, tenant_id: int, raise_on_cap: bool = False) -> Tuple[bool, Optional[str]]:
    return _OK


def check_cpu_hours_limit(db: Session, tenant_id: int, raise_on_limit: bool = False) -> Tuple[bool, Optional[str]]:
    return _OK


def check_tenant_resource_quotas(db: Session, tenant_id: int, additional_storage_bytes: int = 0,
                                 raise_on_limit: bool = False) -> Tuple[bool, Optional[str]]:
    return _OK


def enforce_storage_quota(db: Session, tenant_id: int, incoming_bytes: int, *, user=None) -> None:
    return None


def storage_quota_ok(db: Session, tenant_id: int, incoming_bytes: int) -> bool:
    return True


def count_in_flight_runs(db: Session, tenant_id: int, exclude_run_id: Optional[int] = None) -> int:
    """Live count of in-flight workflow runs for a tenant (QUEUED/RUNNING, not
    capacity-queued)."""
    q = db.query(models.WorkflowRun).filter(
        models.WorkflowRun.tenant_id == tenant_id,
        models.WorkflowRun.is_capacity_queued.is_(False),
        models.WorkflowRun.status.in_([
            models.TaskStatus.QUEUED,
            models.TaskStatus.RUNNING,
        ]),
    )
    if exclude_run_id is not None:
        q = q.filter(models.WorkflowRun.id != exclude_run_id)
    return q.count()


def increment_concurrent_processes(db: Session, tenant_id: int):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if tenant:
        tenant.concurrent_processes_running = (tenant.concurrent_processes_running or 0) + 1
        db.commit()


def decrement_concurrent_processes(db: Session, tenant_id: int):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if tenant and tenant.concurrent_processes_running > 0:
        tenant.concurrent_processes_running -= 1
        db.commit()


def reconcile_concurrent_processes(db: Session, tenant_id: Optional[int] = None) -> Dict[int, Dict[str, int]]:
    """Set each tenant's concurrent_processes_running to its actual in-flight
    run count and audit every change. Returns {tenant_id: {"old", "new"}} for
    changed tenants only. Idempotent."""
    from audit import log_security_event

    rows = (
        db.query(models.WorkflowRun.tenant_id, func.count(models.WorkflowRun.id))
        .filter(
            models.WorkflowRun.status.in_([
                models.TaskStatus.QUEUED,
                models.TaskStatus.RUNNING,
            ]),
            models.WorkflowRun.is_capacity_queued.is_(False),
            models.WorkflowRun.deleted_at.is_(None),
        )
        .group_by(models.WorkflowRun.tenant_id)
        .all()
    )
    actual = {tid: int(cnt) for tid, cnt in rows if tid is not None}

    tenant_q = db.query(models.Tenant).with_for_update()
    if tenant_id is not None:
        tenant_q = tenant_q.filter(models.Tenant.id == tenant_id)

    changed: Dict[int, Dict[str, int]] = {}
    for tenant in tenant_q.all():
        stored = tenant.concurrent_processes_running or 0
        new = actual.get(tenant.id, 0)
        if stored == new:
            continue
        tenant.concurrent_processes_running = new
        db.commit()
        try:
            log_security_event(
                db,
                models.AuditAction.ADMIN_TENANT_MODIFY,
                tenant_id=tenant.id,
                metadata={"event": "concurrent_processes_reconciled", "old": stored, "new": new},
            )
        except Exception:
            logger.exception(
                "reconcile_concurrent_processes: audit write failed for tenant %s (old=%s new=%s)",
                tenant.id, stored, new,
            )
        changed[tenant.id] = {"old": stored, "new": new}
    return changed
