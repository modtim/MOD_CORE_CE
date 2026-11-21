"""CRUD for the worker_management domain (openspec: stack-training-and-prewarm,
Gate 2 per-domain migration, D9).

THE ONLY place `db.query(WorkerLicense | WorkerEnrollment | WorkerEnrollmentToken |
ApiTaskQueue-for-worker-views | ...)` lives for this domain. No tenant_id scoping
here — WorkerLicense/WorkerEnrollment are platform-level rows (owner_tenant_id /
assigned_tenant_ids are ownership metadata on the row, not a row-scoping tenant_id
column), so `services.worker_management_service` owns any ownership/authorization
policy instead of `CRUDBase`'s automatic tenant filter.

Paired with `services/worker_management_service.py` (business logic / policy /
transaction boundary) and the thinned handlers in
`routers/worker_management.py`.

Gate 2b addendum: also holds the read-only lookups for the two deferred
heavy/hot-path handlers, `worker_heartbeat` and `get_platform_capacity` (see
the service module docstring for why their surrounding policy stays in the
router rather than moving here-and-service).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from models import (
    ApiTaskQueue,
    ApiTaskStatus,
    WorkerEnrollment,
    WorkerEnrollmentStatus,
    WorkerEnrollmentToken,
    WorkerLicense,
    WorkerOwnerType,
    WorkerStatus,
)

# BUG-1029-fix2 — "the caller did not supply this value", which is NOT the same
# as an explicit None. Ownership/activity fields default to this so the router's
# heartbeat revive (which omits them) keeps its old keep-existing behaviour,
# while the enroll path can pass an explicit None meaning "write NULL".
_UNSET = object()


# =============================================================================
# WorkerLicense
# =============================================================================


def get_worker_by_name(db: Session, worker_name: str) -> Optional[WorkerLicense]:
    """Fetch one LIVE worker by name. Tenant-un-scoped by design (see module
    docstring) — ownership policy is the service's job, not the fetch's.
    BUG-1029: soft-deleted (tombstoned) rows are invisible here so the same
    worker_name can be re-enrolled after a decommission."""
    return db.query(WorkerLicense).filter(
        WorkerLicense.worker_name == worker_name,
        WorkerLicense.deleted_at.is_(None),
    ).first()


def get_tombstoned_owner_tenant_id(db: Session, worker_name: str) -> Tuple[bool, Optional[int]]:
    """BUG-1029-fix2 (SECURITY) — read-only owner probe for the tombstone that
    currently OWNS `worker_name`.

    Returns `(has_tombstone, owner_tenant_id)`; `owner_tenant_id` is None both
    for a platform-pool tombstone (NULL owner) and when there is no tombstone
    at all, hence the paired flag. `worker_name` is PLAIN unique on
    `worker_licenses`, so at most one row can hold the name. The service
    compares this against the enrollment token's `owner_tenant_id` (NULL==NULL
    counts as equal) and refuses the revive with 409 on a mismatch — the CRUD
    layer stays HTTP-free.
    """
    row = db.query(WorkerLicense).filter(
        WorkerLicense.worker_name == worker_name,
        WorkerLicense.deleted_at.isnot(None),
    ).first()
    if row is None:
        return (False, None)
    return (True, row.owner_tenant_id)


def reactivate_worker_license(
    db: Session,
    worker_name: str,
    *,
    hostname: Optional[str] = None,
    status: WorkerStatus = WorkerStatus.UNLICENSED,
    licensed_at: Optional[datetime] = None,
    first_seen_at: Optional[datetime] = None,
    last_seen_at: Optional[datetime] = None,
    is_remote: Optional[bool] = None,
    owner_type: Optional[WorkerOwnerType] = None,
    owner_tenant_id: Any = _UNSET,
    assigned_tenant_ids: Any = _UNSET,
    queues: Optional[Any] = None,
    hardware_specs: Optional[Any] = None,
    max_concurrent_tasks: Optional[int] = None,
    last_heartbeat: Optional[datetime] = None,
    agent_version: Optional[str] = None,
    network_zone: Optional[str] = None,
    expect_owner_tenant_id: Any = _UNSET,
    reset_activity: bool = False,
) -> Optional[WorkerLicense]:
    """BUG-1029-B1 — reactivate a decommissioned worker instead of INSERTing.

    `worker_name` stays PLAIN unique on `worker_licenses` (it is an FK target:
    persistent_stacks.target_worker, worker_resource_timelines.worker_name), so
    a soft-deleted (tombstoned) row still OWNS the name: a plain INSERT of the
    same name hits the UNIQUE constraint. Every create path for this domain
    therefore calls this BEFORE it builds a new row: if a row with that name
    exists with `deleted_at IS NOT NULL`, it is revived (tombstone cleared) and
    stamped with the fields the create path would have set, and returned.

    Returns None when no tombstone exists (the caller then INSERTs as before).
    LIVE rows are NEVER touched here — the caller's existing duplicate-name
    handling for live rows stays as-is.
    """
    row = (
        db.query(WorkerLicense)
        .filter(
            WorkerLicense.worker_name == worker_name,
            WorkerLicense.deleted_at.isnot(None),
        )
        .first()
    )
    if row is None:
        return None
    # BUG-1029-fix2 SECURITY fail-closed net: the service already refuses the
    # cross-owner revive with 409 (see `get_tombstoned_owner_tenant_id`), but a
    # caller that skips that check must never inherit another owner's row. NULL
    # == NULL (platform pool) counts as equal. Raises a plain ValueError — the
    # CRUD layer stays HTTP-free; the service owns the 409.
    if (
        expect_owner_tenant_id is not _UNSET
        and row.owner_tenant_id is not None
        and row.owner_tenant_id != expect_owner_tenant_id
    ):
        raise ValueError(
            f"tombstone for '{worker_name}' is owned by tenant {row.owner_tenant_id}, "
            f"not {expect_owner_tenant_id}"
        )
    row.deleted_at = None
    row.deleted_by_user_id = None
    row.revoked_at = None
    row.revoked_by_user_id = None
    if hostname is not None:
        row.hostname = hostname
    row.status = status
    row.licensed_at = licensed_at
    if first_seen_at is not None:
        row.first_seen_at = first_seen_at
    if last_seen_at is not None:
        row.last_seen_at = last_seen_at
    if is_remote is not None:
        row.is_remote = is_remote
    if owner_type is not None:
        row.owner_type = owner_type
    # BUG-1029-fix2 SECURITY: on a permitted revive the owner columns come
    # STRONGLY from the token — written even when NULL — so a revived row can
    # never keep a previous owner's tenant (or its assigned-tenant list) behind
    # a fresh enrollment. `_UNSET` (the heartbeat path) keeps the old
    # keep-existing behaviour; an explicit None writes NULL.
    if owner_tenant_id is not _UNSET:
        row.owner_tenant_id = owner_tenant_id
    if assigned_tenant_ids is not _UNSET:
        row.assigned_tenant_ids = assigned_tenant_ids
    if reset_activity:
        # BUG-1029-fix2: a revived worker must not be re-zombied at once on the
        # tombstone's stale heartbeat, and its agent version / network zone
        # belong to the decommissioned machine, not the re-enrolled one, so they
        # are cleared and repopulate on the first heartbeat.
        row.last_heartbeat = last_heartbeat
        row.paused = False
        row.agent_version = None
        row.network_zone = None
    else:
        if last_heartbeat is not None:
            row.last_heartbeat = last_heartbeat
        if agent_version is not None:
            row.agent_version = agent_version
        if network_zone is not None:
            row.network_zone = network_zone
    if queues is not None:
        row.queues = queues
    if hardware_specs is not None:
        row.hardware_specs = hardware_specs
    if max_concurrent_tasks is not None:
        row.max_concurrent_tasks = max_concurrent_tasks
    return row


def reactivate_worker_enrollment(
    db: Session,
    worker_name: str,
    *,
    hostname: Optional[str] = None,
    fingerprint: Optional[Any] = None,
    approval_code: Optional[str] = None,
    bootstrap_token_hash: Optional[str] = None,
    status: Optional[WorkerEnrollmentStatus] = None,
    approved_at: Optional[datetime] = None,
    last_seen_at: Optional[datetime] = None,
    enrollment_token_id: Optional[int] = None,
) -> Optional[WorkerEnrollment]:
    """BUG-1029-B1 twin of `reactivate_worker_license` — see that docstring.

    `worker_enrollments.worker_name` is PLAIN unique (FK target), so a
    decommissioned worker's tombstone still owns the name and the enrollment
    create path cannot INSERT a second row. Returns None when no tombstone
    exists (caller INSERTs); LIVE rows are never touched here.
    """
    row = (
        db.query(WorkerEnrollment)
        .filter(
            WorkerEnrollment.worker_name == worker_name,
            WorkerEnrollment.deleted_at.isnot(None),
        )
        .first()
    )
    if row is None:
        return None
    row.deleted_at = None
    row.deleted_by_user_id = None
    row.revoked_at = None
    row.revoked_by_user_id = None
    if hostname is not None:
        row.hostname = hostname
    if fingerprint is not None:
        row.fingerprint = fingerprint
    if approval_code is not None:
        row.approval_code = approval_code
    if bootstrap_token_hash is not None:
        row.bootstrap_token_hash = bootstrap_token_hash
    if status is not None:
        row.status = status
    row.approved_at = approved_at
    row.approved_by_user_id = None
    if last_seen_at is not None:
        row.last_seen_at = last_seen_at
    if enrollment_token_id is not None:
        row.enrollment_token_id = enrollment_token_id
    return row


def get_worker_by_name_for_update(db: Session, worker_name: str) -> Optional[WorkerLicense]:
    """Row-locked lookup used by the license-assignment race guard
    (`FOR UPDATE` inside the caller's transaction)."""
    return (
        db.query(WorkerLicense)
        .filter(
            WorkerLicense.worker_name == worker_name,
            WorkerLicense.deleted_at.is_(None),
        )
        .with_for_update()
        .first()
    )


def get_worker_by_id(db: Session, worker_id: int) -> Optional[WorkerLicense]:
    return db.query(WorkerLicense).filter(
        WorkerLicense.id == worker_id,
        WorkerLicense.deleted_at.is_(None),
    ).first()


def list_workers_ordered(db: Session) -> List[WorkerLicense]:
    return db.query(WorkerLicense).filter(
        WorkerLicense.deleted_at.is_(None),
    ).order_by(WorkerLicense.worker_name.asc()).all()


def list_all_workers(db: Session) -> List[WorkerLicense]:
    """Unordered `WorkerLicense` scan — used where the original handler took
    whatever order the DB returned (e.g. the NOAH fleet view) so migrating it
    doesn't change response ordering. Tombstones excluded (BUG-1029)."""
    return db.query(WorkerLicense).filter(
        WorkerLicense.deleted_at.is_(None),
    ).all()


def list_active_workers_since(db: Session, cutoff: datetime) -> List[WorkerLicense]:
    """Workers with a heartbeat at/after `cutoff` — used by worker discovery."""
    return db.query(WorkerLicense).filter(
        WorkerLicense.last_heartbeat >= cutoff,
        WorkerLicense.deleted_at.is_(None),
    ).all()


def list_licensed_workers(db: Session) -> List[WorkerLicense]:
    return db.query(WorkerLicense).filter(
        WorkerLicense.status == WorkerStatus.LICENSED,
        WorkerLicense.deleted_at.is_(None),
    ).all()


def count_workers_by_status(db: Session, status: WorkerStatus) -> int:
    return db.query(WorkerLicense).filter(
        WorkerLicense.status == status,
        WorkerLicense.deleted_at.is_(None),
    ).count()


def count_other_active_workers(db: Session, worker: WorkerLicense) -> int:
    """Count LICENSED workers other than `worker` that are neither paused nor
    mid-update — the last-active-worker guard's core query."""
    return db.query(WorkerLicense).filter(
        WorkerLicense.worker_name != worker.worker_name,
        WorkerLicense.status == WorkerStatus.LICENSED,
        WorkerLicense.paused == False,  # noqa: E712 — SQLAlchemy requires == for column comparison
        WorkerLicense.update_requested == False,
        WorkerLicense.deleted_at.is_(None),
    ).count()


def list_offline_zombies(db: Session, cutoff: datetime) -> List[WorkerLicense]:
    return db.query(WorkerLicense).filter(
        WorkerLicense.status == WorkerStatus.OFFLINE,
        WorkerLicense.last_heartbeat < cutoff,
        WorkerLicense.deleted_at.is_(None),
    ).all()


def list_distinct_network_zones(db: Session) -> List[str]:
    rows = (
        db.query(WorkerLicense.network_zone)
        .filter(
            WorkerLicense.network_zone.isnot(None),
            WorkerLicense.deleted_at.is_(None),
        )
        .distinct()
        .all()
    )
    return sorted(set(r[0] for r in rows if r[0]))


def bulk_set_worker_paused(db: Session, worker_name: str, paused: bool) -> int:
    """`UPDATE worker_licenses SET paused = :paused WHERE worker_name = :name`.
    Returns the number of rows matched (0 = unknown worker)."""
    return db.query(WorkerLicense).filter(
        WorkerLicense.worker_name == worker_name,
        WorkerLicense.deleted_at.is_(None),
    ).update({"paused": paused})


def get_hardware_profiles_by_id(db: Session) -> Dict[int, Any]:
    """All HardwareProfile rows, keyed by id (list_workers' bulk profile lookup)."""
    from models import HardwareProfile

    return {p.id: p for p in db.query(HardwareProfile).all()}


def list_worker_claimed_tasks(db: Session, worker_name: str, statuses) -> List[ApiTaskQueue]:
    """`ApiTaskQueue` rows this worker currently holds a claim on, in any of
    `statuses`. Used by the heartbeat's claim-renewal loop — a pure lookup,
    no ordering (the caller iterates unordered exactly as the original
    inline query did)."""
    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.claimed_by_worker == worker_name,
        ApiTaskQueue.status.in_(statuses),
    ).all()


def list_worker_task_ids_by_status(db: Session, worker_name: str, statuses) -> List[int]:
    """Just the ids of this worker's claimed tasks in `statuses` — the
    heartbeat's hard-cancel list (EXPIRED/FAILED/CANCELLED)."""
    return [row.id for row in db.query(ApiTaskQueue.id).filter(
        ApiTaskQueue.claimed_by_worker == worker_name,
        ApiTaskQueue.status.in_(statuses),
    ).all()]


def count_worker_claimed_tasks(db: Session, worker_name: str, statuses) -> int:
    """Count of this worker's claimed tasks in `statuses` (heartbeat's
    `active_task_count` for remote workers)."""
    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.claimed_by_worker == worker_name,
        ApiTaskQueue.status.in_(statuses),
    ).count()


def list_tasks_for_run_and_node(db: Session, run_id, node_id) -> List[ApiTaskQueue]:
    """`ApiTaskQueue` rows for one (workflow_run_id, node_id) pair — the
    dispatch-guard's idempotency/circuit-breaker check in
    `routers/worker_tasks.py:_dispatch_guard`."""
    return (
        db.query(ApiTaskQueue)
        .filter(
            ApiTaskQueue.workflow_run_id == run_id,
            ApiTaskQueue.node_id == node_id,
        )
        .all()
    )


def get_workflow_run_by_id(db: Session, run_id):
    """Fetch one `WorkflowRun` by id. Unscoped by design (internal
    dispatch-loop-failure marker, not a user-facing read). Verbatim
    relocation of the lookup in
    `routers/worker_tasks.py:_fail_run_dispatch_loop`."""
    from models import WorkflowRun

    return db.query(WorkflowRun).filter(WorkflowRun.id == run_id).first()


def list_tasks_by_status(db: Session, status) -> List[ApiTaskQueue]:
    """All `ApiTaskQueue` rows at a single status — no worker scoping.
    Used by the platform-capacity report's pending-tasks pass."""
    return db.query(ApiTaskQueue).filter(ApiTaskQueue.status == status).all()


def list_tasks_in_statuses(db: Session, statuses) -> List[ApiTaskQueue]:
    """All `ApiTaskQueue` rows in any of `statuses` — no worker scoping.
    Used by the platform-capacity report's running/claimed pass."""
    return db.query(ApiTaskQueue).filter(ApiTaskQueue.status.in_(statuses)).all()


def list_completed_tasks_since(db: Session, cutoff: datetime, limit: int) -> List[ApiTaskQueue]:
    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status == ApiTaskStatus.COMPLETED,
        ApiTaskQueue.completed_at >= cutoff,
    ).limit(limit).all()


def list_failed_tasks_since(db: Session, cutoff: datetime, limit: int) -> List[ApiTaskQueue]:
    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status == ApiTaskStatus.FAILED,
        ApiTaskQueue.completed_at >= cutoff,
    ).limit(limit).all()


def list_unrunnable_pending_tasks(db: Session, limit: int) -> List[ApiTaskQueue]:
    """Pending tasks flagged unrunnable (no worker in the fleet can host
    them) — feeds the System Health banner's "add a worker / skip fewer
    variants" summary."""
    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.unrunnable.is_(True),
        ApiTaskQueue.status == ApiTaskStatus.PENDING,
    ).limit(limit).all()


def list_gpu_assignment_tasks(db: Session, card_ids) -> List[tuple]:
    """Raw ``(id, tenant_id, task_payload)`` rows for every ASSIGNED/CLAIMED/
    RUNNING task holding VRAM on any of ``card_ids`` — task-level detail for
    the per-worker GPU usage bar (megaplan 14.8). Mirrors the filter in
    `services.gpu_reservation.cards_in_use` (same JSONB `?` existence check +
    holding statuses) but keeps the task id + full payload instead of
    collapsing to a `Reservation`, so the caller can label each segment by
    task/node_kind.
    """
    from models import ApiTaskQueue, ApiTaskStatus
    from sqlalchemy import func

    wanted = {str(c) for c in card_ids}
    if not wanted:
        return []
    Q = ApiTaskQueue
    rows = (db.query(Q.id, Q.tenant_id, Q.task_payload)
            .filter(Q.status.in_([ApiTaskStatus.ASSIGNED, ApiTaskStatus.CLAIMED, ApiTaskStatus.RUNNING]))
            .filter(func.jsonb_exists(Q.task_payload, "gpu_assignment"))
            .all())
    out = []
    for task_id, tenant_id, payload in rows:
        assignment = (payload or {}).get("gpu_assignment")
        if not isinstance(assignment, dict):
            continue
        if any(str(c.get("card_id") or "") in wanted for c in assignment.get("cards") or ()):
            out.append((task_id, tenant_id, payload))
    return out


def get_resource_timeline_by_worker(db: Session, worker_name: str):
    """The single live `WorkerResourceTimeline` row for a worker (one row
    per worker — upserted every heartbeat with a resource_snapshot)."""
    from models import WorkerResourceTimeline

    return db.query(WorkerResourceTimeline).filter(
        WorkerResourceTimeline.worker_name == worker_name
    ).first()


def list_all_resource_timelines(db: Session):
    from models import WorkerResourceTimeline

    return db.query(WorkerResourceTimeline).all()


def list_approved_active_managers(db: Session):
    """Every mesh `Manager` this API knows about that is both admin-approved
    and currently active — the heartbeat's failover-roster source."""
    from models import Manager

    return db.query(Manager).filter(
        Manager.admin_state == "approved",
        Manager.status == "active",
    ).all()


def get_hardware_profile_by_fingerprint(db: Session, fingerprint: str):
    from models import HardwareProfile

    return db.query(HardwareProfile).filter(HardwareProfile.fingerprint == fingerprint).first()


def get_first_tenant_id(db: Session):
    """`(id,)` row for the lowest-id `Tenant`, or `None` — the auto-calibrate
    hook's last-resort tenant fallback when a worker has no owner/assigned
    tenant to attribute the calibration run to."""
    from models import Tenant

    return db.query(Tenant.id).order_by(Tenant.id).first()


def get_accelerator_profile_by_kind_index(db: Session, hardware_profile_id: int, kind: str, index: int):
    from models import AcceleratorProfile

    return db.query(AcceleratorProfile).filter(
        AcceleratorProfile.hardware_profile_id == hardware_profile_id,
        AcceleratorProfile.kind == kind,
        AcceleratorProfile.index == index,
    ).first()


# =============================================================================
# WorkerEnrollment
# =============================================================================


def get_enrollment_by_name(db: Session, worker_name: str) -> Optional[WorkerEnrollment]:
    """LIVE enrollment only (BUG-1029 soft-delete): a decommissioned worker's
    enrollment row is tombstoned, never reaped, so it must not resolve here."""
    return db.query(WorkerEnrollment).filter(
        WorkerEnrollment.worker_name == worker_name,
        WorkerEnrollment.deleted_at.is_(None),
    ).first()


def list_enrollment_statuses(db: Session) -> Dict[str, str]:
    """`{worker_name: status_value}` for every LIVE enrollment — the bulk map
    `list_workers` merges onto each `WorkerInfo`."""
    return {e.worker_name: e.status.value
            for e in db.query(WorkerEnrollment)
            .filter(WorkerEnrollment.deleted_at.is_(None)).all()}


def list_pending_enrollments_without_license(db: Session) -> List[WorkerEnrollment]:
    """PENDING enrollments that have NO live `WorkerLicense` row of the same name.

    Manual enrolment (`auto_approve=false` / `auto_license` unset) creates the
    `WorkerEnrollment` row only, so it never reaches `list_workers_ordered` and
    `GET /workers` renders nothing the Dashboard's Approve branch can click.
    Tombstoned enrollments (BUG-1029 soft-delete) are excluded the same way
    `list_enrollment_statuses` excludes them.
    """
    licensed = {w.worker_name for w in list_all_workers(db)}
    return [e for e in db.query(WorkerEnrollment)
            .filter(WorkerEnrollment.deleted_at.is_(None),
                    WorkerEnrollment.status == WorkerEnrollmentStatus.PENDING)
            .order_by(WorkerEnrollment.worker_name.asc()).all()
            if e.worker_name not in licensed]


# =============================================================================
# WorkerEnrollmentToken
# =============================================================================


def list_enrollment_tokens(db: Session) -> List[WorkerEnrollmentToken]:
    return db.query(WorkerEnrollmentToken).order_by(WorkerEnrollmentToken.created_at.desc()).all()


def list_unrevoked_enrollment_tokens(db: Session) -> List[WorkerEnrollmentToken]:
    return db.query(WorkerEnrollmentToken).filter(
        WorkerEnrollmentToken.revoked_at.is_(None),
    ).all()


def get_enrollment_token_by_id(db: Session, token_id: int) -> Optional[WorkerEnrollmentToken]:
    return db.query(WorkerEnrollmentToken).filter(WorkerEnrollmentToken.id == token_id).first()


# =============================================================================
# ApiTaskQueue — platform / per-worker queue views
# =============================================================================


def list_in_flight_tasks(db: Session, limit: int) -> List[ApiTaskQueue]:
    return (
        db.query(ApiTaskQueue)
        .filter(ApiTaskQueue.status.in_((ApiTaskStatus.RUNNING, ApiTaskStatus.CLAIMED)))
        .order_by(ApiTaskQueue.claimed_at.asc().nullsfirst(), ApiTaskQueue.id.asc())
        .limit(limit)
        .all()
    )


def list_pending_tasks(db: Session, limit: int) -> List[ApiTaskQueue]:
    return (
        db.query(ApiTaskQueue)
        .filter(ApiTaskQueue.status.in_((ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED)))
        .order_by(ApiTaskQueue.priority.desc(), ApiTaskQueue.id.asc())
        .limit(limit)
        .all()
    )


def queue_status_counts(db: Session) -> Dict[Any, int]:
    from sqlalchemy import func

    return dict(
        db.query(ApiTaskQueue.status, func.count(ApiTaskQueue.id))
        .filter(
            ApiTaskQueue.status.in_(
                (
                    ApiTaskStatus.RUNNING,
                    ApiTaskStatus.CLAIMED,
                    ApiTaskStatus.PENDING,
                    ApiTaskStatus.ASSIGNED,
                )
            )
        )
        .group_by(ApiTaskQueue.status)
        .all()
    )


def queue_pending_counts_by_name(db: Session) -> Dict[Optional[str], int]:
    from sqlalchemy import func

    return dict(
        db.query(ApiTaskQueue.queue_name, func.count(ApiTaskQueue.id))
        .filter(ApiTaskQueue.status.in_((ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED)))
        .group_by(ApiTaskQueue.queue_name)
        .all()
    )


def list_worker_in_flight_tasks(db: Session, worker_name: str) -> List[ApiTaskQueue]:
    return (
        db.query(ApiTaskQueue)
        .filter(
            ApiTaskQueue.claimed_by_worker == worker_name,
            ApiTaskQueue.status.in_([ApiTaskStatus.RUNNING, ApiTaskStatus.CLAIMED]),
        )
        .order_by(ApiTaskQueue.claimed_at.asc().nullsfirst())
        .all()
    )


def list_worker_assigned_tasks(db: Session, worker_name: str) -> List[ApiTaskQueue]:
    return (
        db.query(ApiTaskQueue)
        .filter(
            ApiTaskQueue.assigned_to_worker == worker_name,
            ApiTaskQueue.status.in_([ApiTaskStatus.PENDING, ApiTaskStatus.ASSIGNED]),
        )
        .order_by(ApiTaskQueue.priority.desc(), ApiTaskQueue.id.asc())
        .all()
    )


def clear_worker_task_assignment_fks(db: Session, worker_name: str) -> None:
    """Null out `assigned_to_worker` / `claimed_by_worker` on every task
    pointing at `worker_name` (part of the delete-worker cascade)."""
    db.query(ApiTaskQueue).filter(
        ApiTaskQueue.assigned_to_worker == worker_name
    ).update({ApiTaskQueue.assigned_to_worker: None}, synchronize_session=False)
    db.query(ApiTaskQueue).filter(
        ApiTaskQueue.claimed_by_worker == worker_name
    ).update({ApiTaskQueue.claimed_by_worker: None}, synchronize_session=False)


# =============================================================================
# Delete-worker cascade (pure data access; the "may we delete this worker at
# all" policy lives in the service)
# =============================================================================


# PersistentStack statuses where the worker is still genuinely hosting (or
# expected to re-host) the stack — deleting the worker out from under one of
# these would orphan live infrastructure, so the service blocks the delete
# with a 409 instead. Terminal statuses (torn_down/failed) are stale pointers
# left over from a stack lifecycle that already ended and are safe to clear.
PERSISTENT_STACK_ACTIVE_STATUSES = ("created", "deploying", "running", "migrating", "stopped")


def list_active_persistent_stacks_for_worker(db: Session, worker_name: str) -> List[Any]:
    """PersistentStack rows still targeting `worker_name` whose status means the
    worker cannot be safely deleted yet (read-only; used by the service's
    pre-delete guard). Domain owned by another module — this file only reads
    the model, never mutates persistent_stacks business logic."""
    from models import PersistentStack

    return (
        db.query(PersistentStack)
        .filter(
            PersistentStack.target_worker == worker_name,
            PersistentStack.status.in_(PERSISTENT_STACK_ACTIVE_STATUSES),
        )
        .all()
    )


def delete_worker_cascade(
    db: Session, worker_name: str, worker, enrollment, deleted_by_user_id: Optional[int] = None
) -> None:
    """BUG-1029-worker-hard-delete (ISO 27001 no-hard-delete): SOFT decommission.
    The WorkerLicense + WorkerEnrollment rows are tombstoned (`deleted_at` +
    `deleted_by_user_id` + status flip), never reaped, and their
    `WorkerAssignmentHistoryRecord` / `WorkerResourceTimeline` rows are left
    intact so the worker stays auditable. FK pointers (ApiTaskQueue,
    PersistentStack.target_worker) are still cleared — those are UPDATEs on
    other domains' rows, not deletes. Caller (the service) has already enforced
    the "not LICENSED" / "not the last worker" / "no active persistent stacks
    still targeting this worker" policy and owns the commit."""
    from models import (
        HardwareProfile,
        PersistentStack,
        TaskStatus,
        WorkerEnrollmentStatus,
        WorkflowRun,
    )

    now = datetime.now(timezone.utc)

    # `worker_name` stays PLAIN unique (FK targets: persistent_stacks.target_worker,
    # worker_resource_timelines.worker_name — Postgres needs a plain UNIQUE for an
    # FK), so re-enrolling a decommissioned worker under the SAME name has to
    # reactivate the tombstone in the enrollment path (services scope). The row
    # itself is never reaped.
    clear_worker_task_assignment_fks(db, worker_name)

    # `persistent_stacks.target_worker` has a real FK to worker_licenses.worker_name
    # (no ON DELETE clause). The service has already confirmed no ACTIVE stack
    # still targets this worker, so any rows still pointing at it here are
    # stale (torn_down/failed) — clear the pointer so the worker row can go.
    # We do not touch the persistent_stacks row otherwise (owned elsewhere).
    db.query(PersistentStack).filter(
        PersistentStack.target_worker == worker_name
    ).update({PersistentStack.target_worker: None}, synchronize_session=False)

    # ISO 27001: WorkerAssignmentHistoryRecord rows are NEVER deleted — the
    # assignment history stays attached to the tombstoned worker row.
    if worker:
        if worker.hardware_profile_id:
            for pr in db.query(WorkflowRun).filter(
                WorkflowRun.parameters["is_profiling"].as_boolean() == True,
                WorkflowRun.parameters["profiling_worker"].as_string() == worker_name,
                WorkflowRun.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
            ).all():
                pr.status = TaskStatus.FAILED
                pr.completed_at = datetime.now(timezone.utc)
            profile = db.query(HardwareProfile).filter(
                HardwareProfile.id == worker.hardware_profile_id
            ).first()
            if profile and profile.benchmark_status == "running":
                profile.benchmark_status = "pending"

    # ISO 27001 no-hard-delete: the worker + enrollment rows are TOMBSTONED, not
    # reaped. `worker_name` stays PLAIN unique on both tables (it is an FK target
    # — persistent_stacks.target_worker and worker_resource_timelines.worker_name
    # — and Postgres refuses a conditional UNIQUE as an FK target), so the
    # tombstone still OWNS the name: the decommission-then-re-add flow works
    # because the create paths REACTIVATE this row (see
    # `reactivate_worker_license` above and the enrollment twin in
    # `services/worker_management_service.py`) instead of INSERTing a second row.
    if enrollment:
        enrollment.deleted_at = now
        enrollment.deleted_by_user_id = deleted_by_user_id
        enrollment.status = WorkerEnrollmentStatus.REVOKED
        if enrollment.revoked_at is None:
            enrollment.revoked_at = now
    if worker:
        worker.deleted_at = now
        worker.deleted_by_user_id = deleted_by_user_id
        worker.status = WorkerStatus.OFFLINE
        if worker.revoked_at is None:
            worker.revoked_at = now


# =============================================================================
# Misc lookups used by a handful of endpoints
# =============================================================================


def get_tenant_by_id(db: Session, tenant_id: int):
    from models import Tenant

    return db.query(Tenant).filter(Tenant.id == tenant_id).first()


def get_hardware_profile_by_id(db: Session, hardware_profile_id: int):
    from models import HardwareProfile

    return db.query(HardwareProfile).filter(HardwareProfile.id == hardware_profile_id).first()


def get_active_profiling_run(db: Session, profile_id: int):
    from models import TaskStatus, WorkflowRun

    return (
        db.query(WorkflowRun)
        .filter(
            WorkflowRun.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
            WorkflowRun.parameters["is_profiling"].as_boolean() == True,
            WorkflowRun.parameters["hardware_profile_id"].as_integer() == profile_id,
        )
        .first()
    )


def list_accelerator_profiles(db: Session, hardware_profile_id: int):
    from models import AcceleratorProfile

    return (
        db.query(AcceleratorProfile)
        .filter(AcceleratorProfile.hardware_profile_id == hardware_profile_id)
        .all()
    )
