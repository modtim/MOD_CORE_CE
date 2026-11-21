"""Data-access layer for `routers/persistent_stacks/workers_fleet.py`
(Gate 2b migration; see `services/_base.py` for the layering contract).

The ONLY place `db.query(models.X)` lives for this slice: the tenant's
managed-stack list (`list_stacks`) and the singleton rebalancer-config row
(`set_rebalancer_config`). Distinct from `crud/persistent_stacks_domain.py`,
which owns the other endpoints in this router package
(`routers/persistent_stacks/persistent_stacks.py`) — this module is NOT
touched by that migration and vice versa.

Both fetches are verbatim extractions of the original inline queries —
same filters, same ordering, same tenant scoping (or lack thereof) — so
behavior is unchanged.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def list_stacks_for_tenant(db: Session, tenant_id: int) -> List["models.PersistentStack"]:
    """`list_stacks`: every managed stack owned by the tenant, newest first."""
    return (
        db.query(models.PersistentStack)
        # ISO 27001 soft-delete: a stack removed with DELETE /stacks/{id} keeps
        # its row (id, compose spec, revisions, audit trail) and moves `status`
        # to "deleted". The directory hides it; nothing else does.
        .filter(models.PersistentStack.tenant_id == tenant_id,
                models.PersistentStack.status != "deleted")
        .order_by(models.PersistentStack.id.desc())
        .all()
    )


def get_rebalancer_config_singleton(db: Session) -> Optional["models.StackRebalancerConfig"]:
    """`set_rebalancer_config`: the one `StackRebalancerConfig` row (singleton
    pattern, `singleton_id == 1`). Platform-wide, not tenant-scoped — matches
    the original inline query exactly."""
    return (
        db.query(models.StackRebalancerConfig)
        .filter(models.StackRebalancerConfig.singleton_id == 1)
        .one_or_none()
    )
