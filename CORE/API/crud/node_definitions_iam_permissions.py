"""node_definitions `iam_permissions` sub-module CRUD — the ONLY place
`db.query(models.X)` runs for `routers/node_definitions_domains/iam_permissions.py`
(Gate 2b migration, openspec: stack-training-and-prewarm).

Two query shapes, both matching the pre-migration inline queries exactly:

  * `list_active_worker_licenses(...)` — the live worker-fleet heartbeat scan
    `/predict-variants` unions hardware tags from. Deliberately **platform-wide,
    NOT tenant-scoped** — the wizard offers a variant if ANY active worker on
    the platform advertises the hardware, not just workers assigned to the
    caller's tenant (matches the original inline query, which never filtered
    on tenant_id here).

  * `get_task_by_id(...)` — tenant-**UNSCOPED** task lookup used by
    `/retry-with-corrections`. Scoping it to `current_user.tenant_id` here
    would collapse the legitimate 404-vs-403 distinction the service layer
    depends on (shared migration rule #5): the service resolves the task by
    id FIRST (404 if it plain doesn't exist), THEN checks
    `task_payload["triggered_by_user_id"] == current_user.id` (403 if it
    exists but isn't the caller's) — exactly as the original handler did.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

import models


def list_active_worker_licenses(db: Session, *, heartbeat_cutoff: datetime) -> List["models.WorkerLicense"]:
    """Licensed, unpaused workers heartbeating since `heartbeat_cutoff`.

    Platform-wide (no tenant filter) — see module docstring.
    """
    return db.query(models.WorkerLicense).filter(
        models.WorkerLicense.status == models.WorkerStatus.LICENSED,
        models.WorkerLicense.paused.is_(False),
        models.WorkerLicense.last_heartbeat >= heartbeat_cutoff,
        models.WorkerLicense.deleted_at.is_(None),
    ).all()


def get_task_by_id(db: Session, task_id: int) -> Optional["models.ApiTaskQueue"]:
    """Fetch an `ApiTaskQueue` row by id, tenant-UNSCOPED.

    Matches the original inline `db.query(ApiTaskQueue).filter(ApiTaskQueue.id
    == ...).first()` call sites used to fetch the task under retry, walk its
    `parent_task_id` chain, and re-fetch the freshly-enqueued retry row. See
    module docstring for why this must stay unscoped (404-vs-403 trap).
    """
    return db.query(models.ApiTaskQueue).filter(models.ApiTaskQueue.id == task_id).first()
