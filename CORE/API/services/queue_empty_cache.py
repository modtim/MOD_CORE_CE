"""Per-tenant cache of active `queue_empty` triggers.

Used to short-circuit the COUNT query in tenants that don't have any
queue_empty triggers — most tenants. Without this gate, every task
completion would issue a COUNT against api_task_queue.

Cache is rebuilt at API startup and refreshed on `queue_empty` trigger
CRUD writes (router calls `refresh_tenant_cache(db)` after commit).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class _CachedQueueEmpty:
    trigger_id: int
    workflow_id: int
    tenant_id: int
    scope: str  # "tenant" | "workflow"
    source_workflow_id: Optional[int]
    cooldown_seconds: int
    created_by_user_id: Optional[int]
    last_fired_at: Optional[datetime] = None


_cache_lock = threading.Lock()
_TENANT_CACHE: dict[int, list[_CachedQueueEmpty]] = {}


def has_queue_empty_triggers(tenant_id: int) -> bool:
    """Cheap O(1) gate before issuing the COUNT query."""
    with _cache_lock:
        entries = _TENANT_CACHE.get(tenant_id)
        return bool(entries)


def get_tenant_entries(tenant_id: int) -> list[_CachedQueueEmpty]:
    with _cache_lock:
        return list(_TENANT_CACHE.get(tenant_id, []))


def refresh_tenant_cache(db) -> None:
    """Rebuild the entire cache from the DB. Called from API startup AND
    from the workflow_triggers router after a successful CRUD write."""
    try:
        from models import WorkflowTrigger, WorkflowTriggerType
    except Exception:
        return

    try:
        rows = (
            db.query(WorkflowTrigger)
            .filter(
                WorkflowTrigger.trigger_type == WorkflowTriggerType.QUEUE_EMPTY,
                WorkflowTrigger.is_active.is_(True),
                WorkflowTrigger.deleted_at.is_(None),
            )
            .all()
        )
    except Exception as e:
        logger.warning("queue_empty_cache: refresh DB query failed: %s", e)
        return

    new_cache: dict[int, list[_CachedQueueEmpty]] = {}
    for row in rows:
        cfg = row.configuration or {}
        scope = cfg.get("scope")
        if scope not in ("tenant", "workflow"):
            continue
        new_cache.setdefault(row.tenant_id, []).append(_CachedQueueEmpty(
            trigger_id=row.id,
            workflow_id=row.workflow_id,
            tenant_id=row.tenant_id,
            scope=scope,
            source_workflow_id=cfg.get("source_workflow_id"),
            cooldown_seconds=int(cfg.get("cooldown_seconds", 300)),
            created_by_user_id=row.created_by_user_id,
            last_fired_at=row.last_triggered_at,
        ))

    with _cache_lock:
        _TENANT_CACHE.clear()
        _TENANT_CACHE.update(new_cache)

    total = sum(len(v) for v in new_cache.values())
    logger.info(
        "queue_empty_cache: refreshed (%d tenants, %d total triggers)",
        len(new_cache), total,
    )


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


def fire_queue_empty_triggers(db, completed_task) -> None:
    """Inspect the queue for `completed_task.tenant_id`. If zero active
    tasks remain (per-trigger scope) AND the trigger is past cooldown,
    fire it. MUST NOT raise — failures here cannot roll back the
    source-task commit.

    Zero-overhead path: tenants with no cached `queue_empty` triggers
    return immediately without issuing any COUNT.
    """
    try:
        from models import ApiTaskQueue, ApiTaskStatus, Workflow, WorkflowRun, WorkflowTrigger

        tenant_id = getattr(completed_task, "tenant_id", None)
        if tenant_id is None:
            return

        if not has_queue_empty_triggers(tenant_id):
            return

        active_statuses = [
            ApiTaskStatus.PENDING,
            ApiTaskStatus.ASSIGNED,
            ApiTaskStatus.CLAIMED,
            ApiTaskStatus.RUNNING,
        ]

        now = datetime.now(timezone.utc)
        entries = get_tenant_entries(tenant_id)

        # Per-tenant COUNT cached across entries with scope="tenant" so we
        # only run it once even if multiple tenant-scoped triggers exist.
        tenant_count: Optional[int] = None
        # Per-workflow COUNT memoization across multiple entries with the
        # same source_workflow_id.
        workflow_counts: dict[int, int] = {}

        from services.workflow_orchestrator import dispatch_trigger_run

        for entry in entries:
            try:
                # Cooldown gate.
                if entry.last_fired_at is not None:
                    elapsed = (now - entry.last_fired_at).total_seconds()
                    if elapsed < entry.cooldown_seconds:
                        continue

                if entry.scope == "tenant":
                    if tenant_count is None:
                        tenant_count = (
                            db.query(ApiTaskQueue)
                            .filter(
                                ApiTaskQueue.tenant_id == tenant_id,
                                ApiTaskQueue.status.in_(active_statuses),
                            )
                            .count()
                        )
                    if tenant_count != 0:
                        continue
                else:  # scope == "workflow"
                    src = entry.source_workflow_id
                    if src is None:
                        continue
                    if src not in workflow_counts:
                        # api_task_queue has no workflow_id column —
                        # join through workflow_runs.
                        workflow_counts[src] = (
                            db.query(ApiTaskQueue)
                            .join(
                                WorkflowRun,
                                ApiTaskQueue.workflow_run_id == WorkflowRun.id,
                            )
                            .filter(
                                ApiTaskQueue.tenant_id == tenant_id,
                                WorkflowRun.workflow_id == src,
                                ApiTaskQueue.status.in_(active_statuses),
                            )
                            .count()
                        )
                    if workflow_counts[src] != 0:
                        continue

                workflow = (
                    db.query(Workflow)
                    .filter(
                        Workflow.id == entry.workflow_id,
                        Workflow.tenant_id == tenant_id,
                    )
                    .first()
                )
                if not workflow:
                    continue

                dispatch_trigger_run(
                    db,
                    workflow=workflow,
                    triggered_by_user_id=entry.created_by_user_id,
                    triggered_by_trigger_id=entry.trigger_id,
                    chain_depth=0,
                )

                trigger_row = (
                    db.query(WorkflowTrigger)
                    .filter(WorkflowTrigger.id == entry.trigger_id)
                    .first()
                )
                if trigger_row:
                    trigger_row.last_triggered_at = now
                    trigger_row.trigger_count = (trigger_row.trigger_count or 0) + 1
                    db.commit()
                # Also update the in-memory entry so cooldown applies
                # before the next refresh.
                entry.last_fired_at = now

            except Exception as fire_exc:
                logger.exception(
                    "queue_empty trigger %s firing failed: %s",
                    entry.trigger_id, fire_exc,
                )
                try:
                    db.rollback()
                except Exception:
                    pass

    except Exception as outer_exc:
        logger.exception(
            "fire_queue_empty_triggers outer failure: %s", outer_exc,
        )
