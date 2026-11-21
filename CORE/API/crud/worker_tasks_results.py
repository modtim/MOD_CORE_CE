"""CRUD layer for the `routers/worker_tasks.py` result-processing /
downstream-notification group (brief 1430, continuation of the R5
raw-query cleanup started in 1428's `worker_tasks_lifecycle.py` and
1429's `worker_tasks_status.py`).

Every function here is a verbatim move of a `db.query(...)` call that
previously lived inline in the router function named in its docstring.
No filter, ordering, locking, or tenant-scoping semantics were changed —
callers keep doing their own commit/flush/exception handling; this module
only builds and executes the query.

A few of these router functions had a query shape IDENTICAL to one already
extracted in `worker_tasks_lifecycle.py` / `worker_tasks_status.py` (the
`ApiTaskQueue` id lookup, the `HardwareProfile`/`AcceleratorProfile` id
lookups, the LICENSED-workers list) or to `crud/worker_management.py`
(`Tenant` by id, `WorkerLicense` by name) — those call sites were pointed
at the existing functions instead of duplicating them here.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from models import ApiTaskQueue, ApiTaskStatus, NodeDefinition, PortOutcome


def get_node_definition_by_id(db: Session, node_id) -> Optional[NodeDefinition]:
    """`_apply_variant_smoke_result` / `_process_trivy_scan_result` — plain
    `NodeDefinition` lookup by id (same shape at both call sites)."""
    return db.query(NodeDefinition).filter(NodeDefinition.id == node_id).first()


def get_active_subscription_for_tenant(db: Session, tenant_id):
    """`_resolve_acceptance_threshold` — the tenant's most recent active
    `Subscription` row (query 2 of 3)."""
    from models import Subscription

    return (
        db.query(Subscription)
        .filter(
            Subscription.tenant_id == tenant_id,
            Subscription.status == "active",
        )
        .order_by(Subscription.created_at.desc())
        .first()
    )


def get_plan_by_id(db: Session, plan_id):
    """`_resolve_acceptance_threshold` — the `Plan` row by id (query 3 of
    3), read for its `acceptance_perf_ratio`."""
    from models import Plan

    return db.query(Plan).filter(Plan.id == plan_id).first()


def get_latest_port_outcome_rows(db: Session, variant: str, tenant_id) -> List[PortOutcome]:
    """`_persist_ref_cuda_ratio` — the most recent `PortOutcome` row for
    this (variant, tenant), to stash the measured ref/cuda ratio onto.
    Preserves the original `order_by(...).limit(1).all()` shape (not
    `.first()`) verbatim."""
    return (
        db.query(PortOutcome)
        .filter(
            PortOutcome.variant == variant,
            PortOutcome.tenant_id == tenant_id,
        )
        .order_by(PortOutcome.created_at.desc())
        .limit(1)
        .all()
    )


def get_persistent_stack_by_id(db: Session, stack_id):
    """`_apply_stack_task_result` — the `PersistentStack` row this worker
    task's result applies to, by id."""
    import models

    return (
        db.query(models.PersistentStack)
        .filter(models.PersistentStack.id == stack_id)
        .first()
    )


def get_accelerator_benchmark_strategy_by_id(db: Session, strategy_id: int):
    """`_store_accelerator_result` — the `AcceleratorBenchmarkStrategy` row
    whose success counter gets bumped after a stored benchmark result."""
    from models import AcceleratorBenchmarkStrategy

    return (
        db.query(AcceleratorBenchmarkStrategy)
        .filter(AcceleratorBenchmarkStrategy.id == strategy_id)
        .first()
    )


def list_completed_accelerator_profiles(db: Session, hardware_profile_id: int):
    """`_store_accelerator_result` — every completed `AcceleratorProfile`
    row for this hardware profile, used to roll the best per-device
    compute/memory-bw/vram scores up onto the `HardwareProfile` summary."""
    from models import AcceleratorProfile

    return (
        db.query(AcceleratorProfile)
        .filter(
            AcceleratorProfile.hardware_profile_id == hardware_profile_id,
            AcceleratorProfile.benchmark_status == "completed",
        )
        .all()
    )


def list_task_node_ids(db: Session, run_id):
    """`_redrive_ready_frontier` — every `node_id` with an `ApiTaskQueue`
    row for this run (query 1 of 4), raw rows (caller builds the set)."""
    return (
        db.query(ApiTaskQueue.node_id)
        .filter(ApiTaskQueue.workflow_run_id == run_id)
        .all()
    )


def list_completed_task_node_ids(db: Session, run_id):
    """`_redrive_ready_frontier` — every `node_id` whose `ApiTaskQueue` row
    for this run is COMPLETED (query 2 of 4), raw rows (caller builds the
    set)."""
    return (
        db.query(ApiTaskQueue.node_id)
        .filter(
            ApiTaskQueue.workflow_run_id == run_id,
            ApiTaskQueue.status == ApiTaskStatus.COMPLETED,
        )
        .all()
    )


def get_latest_completed_task(db: Session, run_id, node_id) -> Optional[ApiTaskQueue]:
    """`_redrive_ready_frontier` — the most recent COMPLETED task for a
    re-drive parent node (query 3 of 4), re-used as the re-entry point for
    `_check_and_dispatch_downstream`."""
    return (
        db.query(ApiTaskQueue)
        .filter(
            ApiTaskQueue.workflow_run_id == run_id,
            ApiTaskQueue.node_id == node_id,
            ApiTaskQueue.status == ApiTaskStatus.COMPLETED,
        )
        .order_by(ApiTaskQueue.id.desc())
        .first()
    )


def count_distinct_node_ids_with_task_for_run(db: Session, run_id) -> int:
    """`_redrive_ready_frontier` — post-re-drive distinct `node_id` count
    for this run (query 4 of 4), diffed against the pre-re-drive count to
    report how many new tasks were enqueued."""
    return (
        db.query(ApiTaskQueue.node_id)
        .filter(ApiTaskQueue.workflow_run_id == run_id)
        .distinct()
        .count()
    )


def get_port_outcome_by_source_variant_version(
    db: Session, source_hash: str, variant: str, porter_version: str,
) -> Optional[PortOutcome]:
    """`_record_port_outcomes` — dedup check before inserting a new
    `PortOutcome` row for this exact (source_hash, variant,
    porter_version) triple."""
    return (
        db.query(PortOutcome)
        .filter(
            PortOutcome.source_hash == source_hash,
            PortOutcome.variant == variant,
            PortOutcome.porter_version == porter_version,
        )
        .first()
    )


def get_tenant_webhook_setting(db: Session, tenant_id):
    """`_notify_workflow_complete` — the tenant's `workflow_webhook_url`
    `TenantSetting` row, if configured (query 1 of 2)."""
    from models import TenantSetting

    return (
        db.query(TenantSetting)
        .filter(
            TenantSetting.tenant_id == tenant_id,
            TenantSetting.key == "workflow_webhook_url",
        )
        .first()
    )


def get_user_by_id(db: Session, user_id):
    """`_notify_workflow_complete` — the triggering `User` row, for the
    email-notification path (query 2 of 2)."""
    from models import User

    return db.query(User).filter(User.id == user_id).first()


def count_eligible_pending_tasks(db: Session, worker_name: str, queues: list) -> int:
    """`detect_and_recover_stalled_fetch_loops` — how many PENDING tasks a
    "green but silent" worker is eligible to claim (pinned to it, or
    unpinned on one of its queues) (query 2 of 3)."""
    from sqlalchemy import and_, or_

    return (
        db.query(ApiTaskQueue)
        .filter(
            ApiTaskQueue.status == ApiTaskStatus.PENDING,
            or_(
                ApiTaskQueue.assigned_to_worker == worker_name,
                and_(
                    ApiTaskQueue.assigned_to_worker.is_(None),
                    ApiTaskQueue.queue_name.in_(queues or []),
                ),
            ),
        )
        .count()
    )


def list_stuck_claimed_tasks(db: Session, worker_name: str, cutoff) -> List[ApiTaskQueue]:
    """`detect_and_recover_stalled_fetch_loops` — tasks this worker CLAIMED
    that have sat with no progress past the stall threshold (query 3 of
    3); back-dated so `cleanup_expired_task_claims` requeues them."""
    return (
        db.query(ApiTaskQueue)
        .filter(
            ApiTaskQueue.claimed_by_worker == worker_name,
            ApiTaskQueue.status == ApiTaskStatus.CLAIMED,
            ApiTaskQueue.claimed_at.isnot(None),
            ApiTaskQueue.claimed_at < cutoff,
        )
        .all()
    )
