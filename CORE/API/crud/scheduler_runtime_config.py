"""crud/scheduler_runtime_config.py — query layer for
routers/scheduler_runtime_config.py (Gate 2b).

Singleton (id=1) row access for the scheduler runtime config. The model has
no `tenant_id`/`deleted_at` — it's a platform-wide singleton config row, not
tenant-scoped — so no scoping is applied here, matching the pre-migration
router body exactly (ported 1:1, not invented).

Note: `services/scheduler_runtime_cache.py` (the DB→env→default cache the
GET endpoint reads through) has its own, pre-existing `db.query` for the same
table. That module is a separate, shared service outside this router's file
scope and is left untouched here.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_config(db: Session) -> Optional[models.SchedulerRuntimeConfig]:
    """The singleton `scheduler_runtime_config` row, or None if never written."""
    return (
        db.query(models.SchedulerRuntimeConfig)
        .filter(models.SchedulerRuntimeConfig.singleton_id == 1)
        .one_or_none()
    )


def create_config(db: Session) -> models.SchedulerRuntimeConfig:
    """Insert the (empty) singleton row. Does NOT commit — caller owns the
    transaction."""
    row = models.SchedulerRuntimeConfig(singleton_id=1)
    db.add(row)
    return row
