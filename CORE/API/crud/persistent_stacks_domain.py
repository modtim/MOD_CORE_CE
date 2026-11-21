"""Persistent-stacks (core sub-module) CRUD (Gate 2b).

The ONLY place `db.query(models.X)` lives for the endpoints migrated out of
`routers/persistent_stacks/persistent_stacks.py` (the Gate-2a core-CRUD
sub-module — distinct from `routers/persistent_stacks/workers_fleet.py`,
which owns `create_stack`/`list_stacks`/`get_stack` and is NOT touched here).

Named `persistent_stacks_domain` (with the `_domain` suffix) to avoid
colliding with `services/stack_wake.py`, the `persistent_stacks` router
package itself, and any other `*_stacks*` module already in this codebase.

Pure data access: no auth/policy decisions, no response shaping, no external
I/O. Every function here is a verbatim extraction of an inline query that used
to live in the router — filters/ordering/limits are preserved EXACTLY so
behavior is unchanged (see SHARED RULES: snapshot must stay byte-identical).

Tenant scoping (where the original query had it) is preserved as an explicit
parameter here rather than inferred, so the 404-vs-403 trap called out in the
SHARED RULES (an unscoped fetch + policy check in the service, never a
silently tenant-scoped fetch that collapses a real 403 into a 404) stays
visible to callers. Several of the original queries were ALREADY tenant-UNscoped
(e.g. `get_stack_by_id`, `list_all_stacks_ordered`) — that is carried over
as-is, not "fixed", per the migration's byte-identical mandate.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from datetime import datetime

from sqlalchemy.orm import Session

import models


# =============================================================================
# PersistentStack
# =============================================================================

def get_stack_by_id(db: Session, stack_id: int) -> Optional[models.PersistentStack]:
    """Tenant-UNscoped fetch by primary key (matches the original inline
    queries at update_stack_idle_override / pin_stack_warm — those endpoints
    never filtered on tenant_id either)."""
    return db.query(models.PersistentStack).filter(models.PersistentStack.id == stack_id).first()


def list_bound_persistent_stacks(db: Session, tenant_id: int) -> List[models.PersistentStack]:
    """node_stack_bindings: every managed stack for the tenant, newest first."""
    return (db.query(models.PersistentStack)
            .filter(models.PersistentStack.tenant_id == tenant_id)
            .order_by(models.PersistentStack.id.desc())
            .all())


def list_stacks_by_names(db: Session, names: Sequence[str]) -> List[models.PersistentStack]:
    """managed_ai_status `existing` lookup / managed_ids lookup — same query
    shape used by both (tenant-UNscoped, matching the original)."""
    return db.query(models.PersistentStack).filter(models.PersistentStack.name.in_(list(names))).all()


def get_stack_by_name_and_tenant(db: Session, name: str, tenant_id: int) -> Optional[models.PersistentStack]:
    """provision_managed_ai: idempotent create/update lookup."""
    return (db.query(models.PersistentStack)
            .filter(models.PersistentStack.name == name,
                    models.PersistentStack.tenant_id == tenant_id)
            .first())


def list_stacks_by_tenant(db: Session, tenant_id: int) -> List[models.PersistentStack]:
    """placement_overview: tenant's stacks, unordered (matches the original —
    no `order_by` was ever applied here)."""
    return db.query(models.PersistentStack).filter(models.PersistentStack.tenant_id == tenant_id).all()


def list_stacks_with_mesh_endpoints(db: Session) -> List[models.PersistentStack]:
    """mesh_reachability: every stack that has minted mesh endpoints."""
    return db.query(models.PersistentStack).filter(models.PersistentStack.mesh_endpoints.isnot(None)).all()


def list_all_stacks_ordered(db: Session) -> List[models.PersistentStack]:
    """get_llm_idle_settings: every stack platform-wide, ordered by id."""
    return db.query(models.PersistentStack).order_by(models.PersistentStack.id).all()


# =============================================================================
# WorkerLicense
# =============================================================================

def get_licensed_worker_by_name(db: Session, worker_name: str) -> Optional[models.WorkerLicense]:
    """deploy_stack / backfill_stack / migrate_stack: a specific, LICENSED
    worker (an unlicensed or unknown name yields None)."""
    return (db.query(models.WorkerLicense)
            .filter(models.WorkerLicense.worker_name == worker_name,
                    models.WorkerLicense.status == models.WorkerStatus.LICENSED,
                    models.WorkerLicense.deleted_at.is_(None))
            .first())


def get_worker_by_name(db: Session, worker_name: str) -> Optional[models.WorkerLicense]:
    """set_host_pin: any worker by name — status is NOT filtered here (the
    original allows pinning ahead of a worker becoming available/licensed)."""
    return db.query(models.WorkerLicense).filter(
        models.WorkerLicense.worker_name == worker_name,
        models.WorkerLicense.deleted_at.is_(None)).first()


def first_licensed_worker(db: Session) -> Optional[models.WorkerLicense]:
    """deploy_stack: fleet sanity check — is there ANY licensed worker at all."""
    return db.query(models.WorkerLicense).filter(
        models.WorkerLicense.status == models.WorkerStatus.LICENSED,
        models.WorkerLicense.deleted_at.is_(None)).first()


def list_licensed_workers(db: Session) -> List[models.WorkerLicense]:
    """placement_overview / deploy_stack (capacity-warning candidates): every
    LICENSED worker."""
    return db.query(models.WorkerLicense).filter(
        models.WorkerLicense.status == models.WorkerStatus.LICENSED,
        models.WorkerLicense.deleted_at.is_(None)).all()


def list_workers_by_names(db: Session, names: Sequence[str]) -> List[models.WorkerLicense]:
    """mesh_reachability: the workers hosting the mesh-endpoint stacks."""
    return db.query(models.WorkerLicense).filter(
        models.WorkerLicense.deleted_at.is_(None),
        models.WorkerLicense.worker_name.in_(list(names))).all()


# =============================================================================
# Config singletons
# =============================================================================

def get_rebalancer_config_row(db: Session) -> Optional[models.StackRebalancerConfig]:
    return db.query(models.StackRebalancerConfig).filter(
        models.StackRebalancerConfig.singleton_id == 1).one_or_none()


def get_reconciler_config_row(db: Session) -> Optional[models.StackReconcilerConfig]:
    return db.query(models.StackReconcilerConfig).filter(
        models.StackReconcilerConfig.singleton_id == 1).one_or_none()


# =============================================================================
# ApiTaskQueue
# =============================================================================

def find_stuck_pending_deploy_task(db: Session, stack_id: int) -> Optional[models.ApiTaskQueue]:
    """resolve_stack_endpoint: the most recent unrunnable PENDING deploy task
    for this stack, if any."""
    return (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                    models.ApiTaskQueue.status == models.ApiTaskStatus.PENDING,
                    models.ApiTaskQueue.unrunnable.is_(True))
            .order_by(models.ApiTaskQueue.id.desc()).first())


def list_outstanding_deploy_tasks(db: Session, stack_id: int) -> List[models.ApiTaskQueue]:
    """teardown_stack: unclaimed deploy tasks to cancel."""
    return (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                    models.ApiTaskQueue.task_payload["stack_op"].as_string() == "deploy",
                    models.ApiTaskQueue.status.in_([models.ApiTaskStatus.PENDING,
                                                    models.ApiTaskStatus.ASSIGNED]))
            .all())


def count_inflight_deploy_tasks(db: Session, stack_id: int) -> int:
    """teardown_stack: deploy tasks already claimed/running that cannot be
    recalled."""
    return (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                    models.ApiTaskQueue.task_payload["stack_op"].as_string() == "deploy",
                    models.ApiTaskQueue.status.in_([models.ApiTaskStatus.CLAIMED,
                                                    models.ApiTaskStatus.RUNNING]))
            .count())


def list_nonterminal_deploy_tasks(db: Session, stack_id: int,
                                  exclude_task_id: Optional[int] = None,
                                  created_not_after: Optional[datetime] = None
                                  ) -> List[models.ApiTaskQueue]:
    """stack-stop-release: every deploy/migrate task for `stack_id` not yet in
    a terminal state (PENDING/ASSIGNED/CLAIMED/RUNNING) — the set that must be
    cancelled when a stop/teardown/new-deploy supersedes it, so its
    gpu_assignment stops counting toward gpu_reservation.cards_in_use()
    forever (incident 2026-09-28, stack #27 task 39168). Unlike
    `list_outstanding_deploy_tasks`/`count_inflight_deploy_tasks` (PENDING/
    ASSIGNED-only and deploy-only), this also covers CLAIMED/RUNNING and
    `migrate` — the full set a completed stop/teardown or a fresh deploy
    supersedes. Scoped strictly to `stack_id`; never touches other stacks."""
    q = (db.query(models.ApiTaskQueue)
         .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                 models.ApiTaskQueue.task_payload["stack_op"].as_string().in_(
                     ["deploy", "migrate"]),
                 models.ApiTaskQueue.status.in_([models.ApiTaskStatus.PENDING,
                                                 models.ApiTaskStatus.ASSIGNED,
                                                 models.ApiTaskStatus.CLAIMED,
                                                 models.ApiTaskStatus.RUNNING])))
    if created_not_after is not None:
        # stack-stop-release: "superseded" means created BEFORE the operation that
        # supersedes it. A deploy the user queued AFTER the stop was requested is a
        # NEW intent, not a stale one - cancelling it turns "stop then start" into
        # a stack that never starts. A tie (equal created_at, i.e. both rows
        # inserted in the same Postgres transaction) falls on the cancel side:
        # `<=`, not `<`. No flow dispatches a stop and a deploy in one
        # transaction, so the tie cannot happen on a real stop-then-start.
        q = q.filter(models.ApiTaskQueue.created_at <= created_not_after)
    if exclude_task_id is not None:
        q = q.filter(models.ApiTaskQueue.id != exclude_task_id)
    return q.all()


def get_last_terminal_deploy_task(db: Session, stack_id: int
                                  ) -> Optional[models.ApiTaskQueue]:
    """996 — the most recently finished deploy/migrate task for `stack_id`
    (COMPLETED/FAILED/EXPIRED/CANCELLED), newest first. `stack.target_worker`
    only ever moves on a SUCCESS (see routers/worker_tasks.py's completion
    handler), so after a failed attempt it still names the last host that
    worked, not the host that just failed on it. This is how the sticky
    redeploy guard in `persistent_stacks_domain_service.deploy_stack` tells
    "last host is fine, the failure was on a different host" from "we just
    tried this exact host and it broke" without a schema change."""
    return (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                    models.ApiTaskQueue.task_payload["stack_op"].as_string().in_(
                        ["deploy", "migrate"]),
                    models.ApiTaskQueue.status.in_([models.ApiTaskStatus.COMPLETED,
                                                    models.ApiTaskStatus.FAILED,
                                                    models.ApiTaskStatus.EXPIRED,
                                                    models.ApiTaskStatus.CANCELLED]))
            .order_by(models.ApiTaskQueue.id.desc())
            .first())


# =============================================================================
# Bench (27.6.s3)
# =============================================================================

def list_inflight_bench_tasks(db: Session, stack_id: int) -> List[models.ApiTaskQueue]:
    """27.6.s3: every non-terminal `bench` stack task for `stack_id`.

    The admin-triggered bench trigger refuses while one of these exists — two
    sweeps on the same live stack measure each other (the second one's TTFT
    numbers are the first one's decode load), so the second request is a
    question that has no honest answer, not a question to answer twice.

    Same non-terminal set as :func:`list_nonterminal_deploy_tasks`
    (PENDING/ASSIGNED/CLAIMED/RUNNING): a bench task that has been claimed but
    not yet reported is still in flight, and a PENDING one is about to run.
    Scoped strictly by `stack_id` — never another stack's tasks.
    """
    return (db.query(models.ApiTaskQueue)
            .filter(models.ApiTaskQueue.task_payload["stack_id"].as_integer() == stack_id,
                    models.ApiTaskQueue.task_payload["stack_op"].as_string() == "bench",
                    models.ApiTaskQueue.status.in_([models.ApiTaskStatus.PENDING,
                                                    models.ApiTaskStatus.ASSIGNED,
                                                    models.ApiTaskStatus.CLAIMED,
                                                    models.ApiTaskStatus.RUNNING]))
            .all())


# =============================================================================
# Manager (mesh)
# =============================================================================

def find_approved_active_peer_manager(db: Session, zone: str, exclude_manager_id: str) -> Optional[models.Manager]:
    """resolve_stack_endpoint: the approved, active manager owning `zone`."""
    return (db.query(models.Manager)
            .filter(models.Manager.region == zone,
                    models.Manager.admin_state == "approved",
                    models.Manager.status == "active",
                    models.Manager.manager_id != exclude_manager_id)
            .first())


# =============================================================================
# AcceleratorProfile
# =============================================================================

def distinct_accelerator_profile_kinds(db: Session) -> List[str]:
    return [row[0] for row in db.query(models.AcceleratorProfile.kind).distinct().all()]


# =============================================================================
# StackObservation
# =============================================================================

def list_recent_stack_observations(db: Session, limit: int = 500) -> List[models.StackObservation]:
    return (db.query(models.StackObservation)
            .order_by(models.StackObservation.observed_at.desc())
            .limit(limit).all())


# =============================================================================
# AuditEvent (egress observations)
# =============================================================================

def list_egress_observation_events(
    db: Session, event_name: str, *, stack_id: Optional[int] = None, limit: int = 500
) -> List[models.AuditEvent]:
    q = db.query(models.AuditEvent).filter(models.AuditEvent.event_name == event_name)
    if stack_id is not None:
        q = q.filter(models.AuditEvent.details["stack_id"].as_integer() == stack_id)
    return q.order_by(models.AuditEvent.timestamp.desc()).limit(limit).all()


# =============================================================================
# TenantSetting
# =============================================================================

def list_tenant_settings_by_key(db: Session, key: str) -> List[models.TenantSetting]:
    return db.query(models.TenantSetting).filter(models.TenantSetting.key == key).all()
