"""Platform health CRUD (Gate 2b migration of routers/platform_health.py).

Query layer only: the single licensed-worker count used by the `workers`
component probe. Not tenant-scoped (this is a platform-wide health signal,
not a per-tenant read). BUG-1029b: `WorkerLicense` now carries a soft-delete
`deleted_at` tombstone (migration 20261029a), so this probe filters
`deleted_at IS NULL` to match crud/worker_management.py — tombstoned workers
must not inflate the platform-health total.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

import models


def count_licensed_workers(db: Session) -> int:
    """Count workers whose license status is LICENSED (healthy + assigned)."""
    return db.query(models.WorkerLicense).filter(
        models.WorkerLicense.deleted_at.is_(None),
        models.WorkerLicense.status == models.WorkerStatus.LICENSED,
    ).count()
