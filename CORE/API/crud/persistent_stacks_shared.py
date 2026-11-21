"""Persistent-stacks `_shared` module CRUD (Gate 2b).

The ONLY place `db.query(models.X)` lives for the helper functions that used
to inline their queries in `routers/persistent_stacks/_shared.py`. `_shared`
is the package's shared-helpers module: it defines no HTTP endpoints of its
own, but many of its module-level helpers (worker/stack lookups, the
managed-AI placement chain, ingress/TLS/idle-settings policy) are imported by
every sibling sub-module and by `routers/persistent_stacks/__init__.py`.

Pure data access: no auth/policy decisions, no response shaping, no external
I/O. Every function here is a verbatim extraction of an inline query that used
to live in `_shared.py` — filters/ordering/limits are preserved EXACTLY so
behavior is unchanged (contract snapshot + runtime behavior stay identical).

Several of the original queries were tenant-UNscoped (e.g. `get_stacks_by_names`,
`get_exposed_stacks`) — that is carried over as-is, not "fixed", per the
migration's byte-identical mandate. `get_owned_stack` IS tenant-scoped in the
query itself (not a 404-vs-403 split) because that already matches the
original `_get_owned` behavior: a cross-tenant id has always 404'd here, there
is no separate 403 case to preserve.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

import models


# =============================================================================
# WorkerLicense
# =============================================================================

def get_licensed_worker_in_zone(db: Session, zone: str) -> Optional[models.WorkerLicense]:
    """_pick_worker: a LICENSED worker in the requested home zone, if any."""
    return (db.query(models.WorkerLicense)
            .filter(models.WorkerLicense.status == models.WorkerStatus.LICENSED,
                    models.WorkerLicense.network_zone == zone,
                    models.WorkerLicense.deleted_at.is_(None))
            .first())


def get_first_licensed_worker(db: Session) -> Optional[models.WorkerLicense]:
    """_pick_worker fallback: any LICENSED worker."""
    return (db.query(models.WorkerLicense)
            .filter(models.WorkerLicense.status == models.WorkerStatus.LICENSED,
                    models.WorkerLicense.deleted_at.is_(None))
            .first())


def get_licensed_workers(db: Session) -> List[models.WorkerLicense]:
    """_fleet_capacity_workers: every LICENSED worker."""
    return (db.query(models.WorkerLicense)
            .filter(models.WorkerLicense.status == models.WorkerStatus.LICENSED,
                    models.WorkerLicense.deleted_at.is_(None))
            .all())


def get_worker_by_name(db: Session, worker_name: str) -> Optional[models.WorkerLicense]:
    """_fleet_capacity_workers (embedded-worker fallback) / _host_device_summary /
    host_pin_status: a worker row by its unique `worker_name`, regardless of
    status."""
    return (db.query(models.WorkerLicense)
            .filter(models.WorkerLicense.worker_name == worker_name,
                    models.WorkerLicense.deleted_at.is_(None))
            .first())


# =============================================================================
# PersistentStack
# =============================================================================

def get_owned_stack(db: Session, stack_id: int, tenant_id: int) -> Optional[models.PersistentStack]:
    """_get_owned: tenant-scoped fetch by primary key (matches the original —
    a cross-tenant id has always 404'd here, not 403'd)."""
    return (db.query(models.PersistentStack)
            .filter(models.PersistentStack.id == stack_id,
                    models.PersistentStack.tenant_id == tenant_id)
            .first())


def get_first_stack_by_names(db: Session, names: Sequence[str]) -> Optional[models.PersistentStack]:
    """reconcile_managed_ai: existence probe — any one managed-AI stack already
    provisioned (tenant-UNscoped, `.first()` not `.all()` — matches the
    original, which never needed the full list)."""
    return (db.query(models.PersistentStack)
            .filter(models.PersistentStack.name.in_(list(names)))
            .first())


def get_stacks_by_names(db: Session, names: Sequence[str]) -> List[models.PersistentStack]:
    """apply_managed_ai_core / reconcile_managed_ai: every stack (any tenant)
    whose name is in `names` — the managed-AI stack-name set. Tenant-UNscoped
    in the original: VRAM held on a card is a fleet fact, not a tenancy one."""
    return (db.query(models.PersistentStack)
            .filter(models.PersistentStack.name.in_(list(names)))
            .all())


def get_stack_by_name_and_tenant(db: Session, name: str, tenant_id: int) -> Optional[models.PersistentStack]:
    """apply_managed_ai_core: idempotent create/update lookup for one managed-AI
    stack definition, scoped to the applying user's tenant."""
    return (db.query(models.PersistentStack)
            .filter(models.PersistentStack.name == name,
                    models.PersistentStack.tenant_id == tenant_id)
            .first())


def get_exposed_stacks(db: Session) -> List[models.PersistentStack]:
    """_ingress_authorized_domain source (1): every publicly-exposed stack
    (tenant-UNscoped — the ingress `ask` gate must authorise across tenants).

    A soft-deleted stack can carry `exposed=True` from before it was deleted
    (exposure may be set on a never-deployed stack) — exclude it, or a
    deleted stack would keep authorising TLS issuance for its old hostname."""
    return (db.query(models.PersistentStack)
            .filter(models.PersistentStack.exposed.is_(True),
                    models.PersistentStack.status != "deleted")
            .all())


def get_stacks_with_tls_config(db: Session) -> List[models.PersistentStack]:
    """_ingress_authorized_domain source (2): every stack carrying a per-service
    x-mod.tls declaration (legacy auto-issuance path), tenant-UNscoped."""
    return (db.query(models.PersistentStack)
            .filter(models.PersistentStack.tls_config.isnot(None),
                    models.PersistentStack.status != "deleted")
            .all())


# =============================================================================
# ApiTaskQueue
# =============================================================================

def get_stuck_unrunnable_task(db: Session, stack_id: int) -> Optional[models.ApiTaskQueue]:
    """_unrunnable_reason_for: the most recent PENDING+unrunnable dispatch for
    this stack, if the placement sweep flagged one."""
    return (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                    models.ApiTaskQueue.status == models.ApiTaskStatus.PENDING,
                    models.ApiTaskQueue.unrunnable.is_(True))
            .order_by(models.ApiTaskQueue.id.desc())
            .first())


# =============================================================================
# User
# =============================================================================

def get_platform_admin_owner(db: Session, tenant_id: int) -> Optional[models.User]:
    """reconcile_managed_ai: the platform-admin owner of the managed-AI stacks
    (tenant 1's admin — "nobody to own it" pre-first-signup)."""
    return (db.query(models.User)
            .filter(models.User.is_platform_admin == True,  # noqa: E712
                    models.User.tenant_id == tenant_id)
            .first())


# =============================================================================
# TenantSetting
# =============================================================================

def get_tenant_setting(db: Session, tenant_id: int, key: str) -> Optional[models.TenantSetting]:
    """_load_tenant_tls_setting: one tenant's setting row by key."""
    return (db.query(models.TenantSetting)
            .filter(models.TenantSetting.tenant_id == tenant_id,
                    models.TenantSetting.key == key)
            .first())


# =============================================================================
# Tenant
# =============================================================================

def get_tenant_by_id(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    """Resolve a tenant row by id."""
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


# =============================================================================
# PlatformLLMIdleSettings
# =============================================================================

def get_idle_settings_by_id(db: Session, settings_id: int = 1) -> Optional[models.PlatformLLMIdleSettings]:
    """_idle_settings_row: the singleton row, if it already exists."""
    return (db.query(models.PlatformLLMIdleSettings)
            .filter(models.PlatformLLMIdleSettings.id == settings_id)
            .first())
