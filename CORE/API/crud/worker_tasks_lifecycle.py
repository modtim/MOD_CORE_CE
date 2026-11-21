"""CRUD layer for the `routers/worker_tasks.py` task-lifecycle group
(claim/report-started/contention-verdict/report-complete/report-failed —
brief 1428, R5 raw-query cleanup).

Every function here is a verbatim move of a `db.query(...)` call that
previously lived inline in the router function of the same name (see each
docstring for which router function it was lifted from). No filter,
ordering, locking, or tenant-scoping semantics were changed — callers keep
doing their own commit/flush and exception handling; this module only
builds and executes the query.

Several of these (`get_node_run_by_id`, `get_workflow_run_by_id`) were
IDENTICAL `db.query(...)` shapes repeated verbatim at multiple call sites
in the router (just parameterized by a different id each time) — they are
consolidated into one function here, called from each site, rather than
duplicated 1:1. This changes nothing about the SQL executed at any call
site.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from models import ApiTaskQueue, ApiTaskStatus, NodeRun, WorkflowRun, NodeDefinition


def get_unruled_drift_variant_rows(db: Session, node_definition_id: int) -> List:
    """`_unruled_drift_variants` — project-agent §1.5 variant rows whose
    sample-test output diverged and nobody has ruled on yet."""
    from models import NodeVariantVerification

    return (
        db.query(NodeVariantVerification.variant)
        .filter(
            NodeVariantVerification.node_definition_id == node_definition_id,
            NodeVariantVerification.verdict == "drift",
            NodeVariantVerification.drift_ruling.is_(None),
        )
        .all()
    )


def get_task_queue_row_by_id(db: Session, task_id: int) -> Optional[ApiTaskQueue]:
    """`_try_claim_task` — fetch the just-claimed `ApiTaskQueue` row by id
    (used for both the scheduler-placed and FIFO-fallback claim paths)."""
    return db.query(ApiTaskQueue).filter(ApiTaskQueue.id == task_id).first()


def count_worker_active_tasks(db: Session, worker_name: str) -> int:
    """`_try_claim_task` — count of CLAIMED/RUNNING tasks already held by
    this worker, for the `max_concurrent_tasks` safety net on the FIFO
    fallback claim path."""
    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.claimed_by_worker == worker_name,
        ApiTaskQueue.status.in_([ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING]),
    ).count()


def get_node_run_by_id(db: Session, node_run_id: int) -> Optional[NodeRun]:
    """Shared by `report_task_started`, `report_task_complete`, and
    `report_task_failed` — fetch the `NodeRun` linked to a task by id."""
    return db.query(NodeRun).filter(NodeRun.id == node_run_id).first()


def get_contention_calibration_tasks(db: Session, pair_id: str) -> List[ApiTaskQueue]:
    """`apply_contention_verdict` — all calibration-run tasks sharing this
    `calibration_pair_id`, so the stressor verdict can be stamped onto
    both sides of the pair."""
    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
        ApiTaskQueue.task_payload["calibration_pair_id"].as_string() == pair_id,
    ).all()


def get_workflow_run_by_id(db: Session, workflow_run_id: int) -> Optional[WorkflowRun]:
    """Shared by `report_task_complete` (bug-25 recovery un-fail check) and
    `report_task_failed` (final-failure propagation) — fetch the parent
    `WorkflowRun` by id."""
    return db.query(WorkflowRun).filter(WorkflowRun.id == workflow_run_id).first()


def cancel_claimable_tasks_for_failed_run(db: Session, workflow_run_id: int, exclude_task_id: int) -> int:
    """`report_task_failed` — delta-dag-wizard-cutover §22.4: bulk-cancel
    every still-claimable (PENDING/ASSIGNED) task of a run that just
    FAILED, other than the failing task itself. Returns the number of
    rows updated. Bulk UPDATE, `synchronize_session=False` preserved
    verbatim from the original call site."""
    return (
        db.query(ApiTaskQueue)
        .filter(
            ApiTaskQueue.workflow_run_id == workflow_run_id,
            ApiTaskQueue.id != exclude_task_id,
            ApiTaskQueue.status.in_([ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED]),
        )
        .update(
            {
                ApiTaskQueue.status: ApiTaskStatus.CANCELLED,
                ApiTaskQueue.error_message: "Cancelled: the workflow run failed before this task started.",
            },
            synchronize_session=False,
        )
    )


def skip_unstarted_node_runs_for_failed_run(db: Session, workflow_run_id: int) -> int:
    """2400LR-nrq — bulk-skip a failed run's never-started NodeRuns.

    BUG 2400LR-nrq (g1R run 79): when a run goes FAILED, the node_runs of its
    downstream nodes stay QUEUED forever, so the run-detail UI shows queued
    nodes on a dead run and anything counting QUEUED node_runs (scheduler,
    dashboards, quotas) over-counts.

    Canonical status is `TaskStatus.BLOCKED` — the same value the existing
    "never ran because upstream failed" cascade already writes (routers/worker_tasks.py:12297
    `nr.status = TaskStatus.BLOCKED` for the failing node's QUEUED descendants),
    and the only one the UI renders for it (CORE/WorkflowBuilder/components/GridView.tsx:53
    `case "blocked": return "bg-orange-400"`; CORE/WorkflowBuilder/lib/types.ts:58
    lists 'blocked' in the node-run status union). TaskStatus has no
    skipped/cancelled member, so BLOCKED is the codebase's skip status.

    One bulk UPDATE, scoped to this run by `workflow_run_id` (node_runs has no
    tenant_id column — models.py:3664) and to the not-started status set. RUNNING
    node_runs are deliberately NOT touched: their task-result path (report_task_complete
    / report_task_failed) owns them, and if one reports in later it writes its own
    terminal status over the run's already-FAILED parent — the run stays FAILED
    (report_task_failed only flips a run at QUEUED/RUNNING, routers/worker_tasks.py:4297).

    ISO: status update only, no deletes. `completed_at` is stamped like the
    drain/pause BLOCKED path (routers/worker_tasks.py:13030-13032).
    """
    from datetime import datetime, timezone
    from models import TaskStatus

    return (
        db.query(NodeRun)
        .filter(
            NodeRun.workflow_run_id == workflow_run_id,
            NodeRun.status == TaskStatus.QUEUED,
        )
        .update(
            {
                NodeRun.status: TaskStatus.BLOCKED,
                NodeRun.completed_at: datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
    )


def get_resource_pattern_for_node_kind(db: Session, node_kind: str, queue_name: str):
    """`report_task_failed` — OOM circuit breaker: the `ResourcePattern`
    row for this node_kind/queue, to halve its confidence on an apparent
    OOM failure."""
    from models import ResourcePattern

    return db.query(ResourcePattern).filter(
        ResourcePattern.node_kind == node_kind,
        ResourcePattern.queue_name == queue_name,
    ).first()


def get_accelerator_profile(db: Session, hardware_profile_id: int, kind: str, index: int):
    """`report_task_failed` — benchmark-failure path: the
    `AcceleratorProfile` row for the (profile, kind, index) being
    benchmarked, so it can be quarantined/classified on failure."""
    from models import AcceleratorProfile

    return db.query(AcceleratorProfile).filter(
        AcceleratorProfile.hardware_profile_id == hardware_profile_id,
        AcceleratorProfile.kind == kind,
        AcceleratorProfile.index == index,
    ).first()


def get_hardware_profile_by_id(db: Session, profile_id: int):
    """`report_task_failed` — system-benchmark failure path: the
    `HardwareProfile` row being benchmarked, by id."""
    from models import HardwareProfile

    return db.query(HardwareProfile).filter(HardwareProfile.id == profile_id).first()


def find_system_node_definition(db: Session, node_kind: str) -> Optional[NodeDefinition]:
    """`_task_is_system_build` — the SYSTEM-shared (tenant_id IS NULL)
    `NodeDefinition` for this node_kind, if one exists."""
    return db.query(NodeDefinition).filter(
        NodeDefinition.node_kind == node_kind,
        NodeDefinition.tenant_id.is_(None),
    ).first()
