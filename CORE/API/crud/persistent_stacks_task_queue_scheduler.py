"""Data-access layer for `routers/persistent_stacks/task_queue_scheduler.py`
(Gate 2b migration; see `services/_base.py` for the layering contract).

The ONLY place `db.query(models.X)` lives for this slice: the per-worker
"which stacks currently hold this worker's accelerators" lookup
(`accelerator_capacity`) and the singleton `StackReconcilerConfig` row
(`set_reconciler_config`). Distinct from `crud/persistent_stacks_domain.py`
and `crud/persistent_stacks_workers_fleet.py`, which own other endpoints in
this router package — not touched by this migration and vice versa.

Both are verbatim extractions of the original inline queries.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

import models


def list_stack_holders_for_worker(db: Session, worker_name: str) -> List[Tuple[int, str]]:
    """`accelerator_capacity`: (id, name) of every stack currently occupying
    accelerators on `worker_name` (running/deploying/migrating)."""
    return (
        db.query(models.PersistentStack.id, models.PersistentStack.name)
        .filter(
            models.PersistentStack.target_worker == worker_name,
            models.PersistentStack.status.in_(("running", "deploying", "migrating")),
        )
        .all()
    )


def get_reconciler_config_singleton(db: Session) -> Optional["models.StackReconcilerConfig"]:
    """`set_reconciler_config`: the one `StackReconcilerConfig` row (singleton
    pattern, `singleton_id == 1`). Platform-wide, not tenant-scoped — matches
    the original inline query exactly."""
    return (
        db.query(models.StackReconcilerConfig)
        .filter(models.StackReconcilerConfig.singleton_id == 1)
        .one_or_none()
    )
