"""LLM Rate Limits CRUD — the ONLY place `db.query(...)` lives for the
platform/tenant LLM rate-limit admin domain (`routers/llm_rate_limits.py`).

Platform-admin gating and business policy (get-or-create semantics, field
merge/clear rules, rejection grouping, tenant-usage-view gating) live in
`services/llm_rate_limits_service.py`; every function below is a plain query
or write primitive with no policy.

Tenant-scoping note: `PlatformLLMRateLimits` is a platform-wide singleton
(id=1) — no tenant_id, nothing to scope. `TenantLLMRateLimits` is keyed
directly by `tenant_id` (its own primary key) and has no soft-delete column.
`LLMRateLimitRejection` is an append-only, platform-wide observability log
(no soft-delete column) — nothing here ever deletes a row from it.

`get_tenant` is intentionally NOT `deleted_at`-filtered, matching the
pre-migration query exactly — see the router's 404-vs-403 note in
`services/llm_rate_limits_service.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_platform_row(db: Session) -> Optional[models.PlatformLLMRateLimits]:
    """The singleton platform-defaults row (id=1), or None if never created."""
    return db.query(models.PlatformLLMRateLimits).filter(
        models.PlatformLLMRateLimits.id == 1).first()


def create_platform_row(db: Session) -> models.PlatformLLMRateLimits:
    """Insert the singleton platform-defaults row with column defaults.
    Commits (matches pre-migration behaviour: created lazily on first read)."""
    row = models.PlatformLLMRateLimits(id=1)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_tenant_overrides(db: Session) -> List[models.TenantLLMRateLimits]:
    """Every tenant that has ever set an override. Small, platform-admin-only
    table — unbounded on purpose, matching pre-migration behaviour."""
    return db.query(models.TenantLLMRateLimits).all()


def get_tenant(db: Session, tenant_id: int) -> Optional[models.Tenant]:
    """Existence check only, tenant-UN-scoped (a platform admin may target
    any tenant) and NOT `deleted_at`-filtered — matches the pre-migration
    query exactly."""
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


def get_tenant_override(db: Session, tenant_id: int) -> Optional[models.TenantLLMRateLimits]:
    return db.query(models.TenantLLMRateLimits).filter(
        models.TenantLLMRateLimits.tenant_id == tenant_id).first()


def create_tenant_override(db: Session, tenant_id: int) -> models.TenantLLMRateLimits:
    """Insert a new (all-inherit) override row. Does NOT commit — the caller
    (the service) owns the transaction so the field writes that follow land
    in the same commit as the insert."""
    row = models.TenantLLMRateLimits(tenant_id=tenant_id)
    db.add(row)
    return row


def list_rejections(db: Session, cutoff: datetime, limit: int) -> List[models.LLMRateLimitRejection]:
    """Bounded, most-recent-first slice of rejections at/after `cutoff`."""
    return (db.query(models.LLMRateLimitRejection)
            .filter(models.LLMRateLimitRejection.occurred_at >= cutoff)
            .order_by(models.LLMRateLimitRejection.occurred_at.desc())
            .limit(limit).all())
