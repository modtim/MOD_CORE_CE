"""CRUD layer for the `routers/worker_tasks.py` status/logs/entitlement/
cache-shortcut group (brief 1429, continuation of the R5 raw-query cleanup
started in 1428's `worker_tasks_lifecycle.py`).

Every function here is a verbatim move of a `db.query(...)` call that
previously lived inline in the router function named in its docstring. No
filter, ordering, or short-circuit semantics were changed — callers keep
doing their own commit/flush/exception handling; this module only builds
and executes the query (or, where two queries are tightly coupled with a
fallback relationship, both of them in the same order as before).

A few of these router functions already had an IDENTICAL `db.query(...)`
shape to one already extracted in `worker_tasks_lifecycle.py` (e.g. the
plain `ApiTaskQueue` id lookup, the plain `WorkflowRun` id lookup, the
plain `NodeRun` id lookup, the worker's claimed/running task count) — the
router call sites for those were pointed at the existing
`worker_tasks_lifecycle` functions instead of duplicating them here.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from models import ApiTaskQueue, ApiTaskStatus, NodeDefinition, WorkerLicense, WorkerStatus


def get_cog_by_id(db: Session, cog_id: int):
    """`resolve_cog_hash` — the Cog row by id (query 1 of 3)."""
    from models import Cog
    return db.query(Cog).filter(Cog.id == cog_id).first()


def get_version_by_id(db: Session, version_id: int):
    """`resolve_cog_hash` — the Cog's latest Version row by id (query 2 of 3)."""
    from models import Version
    return db.query(Version).filter(Version.id == version_id).first()


def get_content_blob(db: Session, content_id, tenant_id: int):
    """`resolve_cog_hash` — the Version's ContentBlob, tenant-scoped (query 3 of 3)."""
    from models import ContentBlob
    return db.query(ContentBlob).filter(
        ContentBlob.id == content_id,
        ContentBlob.tenant_id == tenant_id,
    ).first()


def list_system_tasks_page(db: Session, offset: int, limit: int) -> Tuple[int, List[ApiTaskQueue]]:
    """`list_system_tasks` — platform-admin-only page of system/platform
    tasks (workflow_run_id IS NULL), newest id first. Returns
    (total_count, page_of_tasks), preserving the original count()-then-
    offset/limit()-then-all() order."""
    from sqlalchemy import desc

    query = db.query(ApiTaskQueue).filter(
        ApiTaskQueue.workflow_run_id.is_(None),
    ).order_by(desc(ApiTaskQueue.id))

    total = query.count()
    tasks = query.offset(offset).limit(limit).all()
    return total, tasks


def list_child_tasks_for_workflow_run(db: Session, workflow_run_id: int) -> List[ApiTaskQueue]:
    """`get_workflow_run_container_logs` — every child `ApiTaskQueue` row
    for an aggregated-build workflow run, oldest first."""
    return (
        db.query(ApiTaskQueue)
        .filter(ApiTaskQueue.workflow_run_id == workflow_run_id)
        .order_by(ApiTaskQueue.id.asc())
        .all()
    )


def count_active_tasks(db: Session) -> int:
    """`active_task_count` — tasks currently RUNNING or CLAIMED across all
    workers (used by update-system.sh to wait for drain before restart)."""
    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.status.in_([ApiTaskStatus.RUNNING, ApiTaskStatus.CLAIMED])
    ).count()


def count_task_attempts_for_node_run(db: Session, node_run_id) -> int:
    """`_attempt_index` — how many task rows this node run has ever had,
    counted from the task rows themselves (not tracked on the node run)."""
    return db.query(ApiTaskQueue).filter(
        ApiTaskQueue.node_run_id == node_run_id
    ).count()


def get_latest_claimed_task_for_node_run(db: Session, node_run_id) -> Optional[ApiTaskQueue]:
    """`_live_checkpoint_host` — the most recent task for this node run that
    was claimed by some worker (query 1 of 2; the checkpoint-affinity
    candidate before its liveness is checked)."""
    return (
        db.query(ApiTaskQueue)
        .filter(ApiTaskQueue.node_run_id == node_run_id,
                ApiTaskQueue.claimed_by_worker.isnot(None))
        .order_by(ApiTaskQueue.id.desc())
        .first()
    )


def get_live_licensed_worker(db: Session, worker_name: str, cutoff) -> Optional[WorkerLicense]:
    """`_live_checkpoint_host` — is that prior-attempt worker still licensed
    and heartbeating within the silence window (query 2 of 2)?"""
    return (
        db.query(WorkerLicense)
        .filter(WorkerLicense.worker_name == worker_name,
                WorkerLicense.status == WorkerStatus.LICENSED,
                WorkerLicense.deleted_at.is_(None),
                WorkerLicense.last_heartbeat.isnot(None),
                WorkerLicense.last_heartbeat >= cutoff)
        .first()
    )


def get_node_def_with_entitlement(db: Session, node_kind, tenant_id):
    """`_check_marketplace_entitlement` — the SYSTEM NodeDefinition of this
    kind, outer-joined to the tenant's non-soft-deleted
    TenantNodeEntitlement. Returns the raw `(NodeDefinition, entitlement_id)`
    row (or None if no system node of this kind exists), exactly as before."""
    from sqlalchemy import and_
    from models import TenantNodeEntitlement

    _nd = NodeDefinition
    _ent = TenantNodeEntitlement
    return (
        db.query(_nd, _ent.id)
        .outerjoin(
            _ent,
            and_(
                _ent.node_definition_id == _nd.id,
                _ent.tenant_id == tenant_id,
                _ent.deleted_at.is_(None),
            ),
        )
        .filter(_nd.node_kind == node_kind, _nd.tenant_id.is_(None))
        .order_by(_nd.id)
        .first()
    )


def get_node_definition_for_tenant(db: Session, node_kind, tenant_id) -> Optional[NodeDefinition]:
    """`_node_def_for_tenant` — tenant-scoped NodeDefinition lookup: live
    rows only, system rows (tenant_id IS NULL) apply to every tenant, the
    tenant's own row wins over the system row (False sorts before True)."""
    return db.query(NodeDefinition).filter(
        NodeDefinition.node_kind == node_kind,
        NodeDefinition.deleted_at.is_(None),
        NodeDefinition.tenant_id.is_(None)
        | (NodeDefinition.tenant_id == tenant_id),
    ).order_by(
        NodeDefinition.tenant_id.is_(None),
    ).first()


def list_licensed_workers(db: Session) -> List[WorkerLicense]:
    """`check_remote_availability` — every LICENSED worker, filtered further
    in Python for queue support / tenant authorization / concurrency."""
    return db.query(WorkerLicense).filter(
        WorkerLicense.status == WorkerStatus.LICENSED,
        WorkerLicense.deleted_at.is_(None),
    ).all()


def get_cache_hit_output_metadata(db: Session, content_hash: str, tenant_id: int) -> Tuple[str, str, int]:
    """`_maybe_cache_short_circuit` — recover the cached output's real file
    metadata (name / extension / size) for a cache-hit row that only stores
    the content hash. Prefer a prior `WorkflowNodeOutput` (has
    logical_name); fall back to `ContentBlob` (has extension/size) only if
    the first lookup left the extension empty — same fallback order as the
    original inline code, just moved. Returns (name, extension, size_bytes),
    each defaulting to "" / "" / 0."""
    from models import WorkflowNodeOutput, ContentBlob

    hit_name, hit_ext, hit_size = "", "", 0
    prior = (
        db.query(WorkflowNodeOutput)
        .filter(WorkflowNodeOutput.content_hash == content_hash,
                WorkflowNodeOutput.tenant_id == tenant_id,
                WorkflowNodeOutput.file_extension != "")
        .order_by(WorkflowNodeOutput.size_bytes.desc(), WorkflowNodeOutput.id.desc())
        .first()
    )
    if prior is not None:
        hit_name = prior.logical_name or ""
        hit_ext = prior.file_extension or ""
        hit_size = prior.size_bytes or 0
    if not hit_ext:
        # ContentBlob's PK `id` IS the SHA256 hash (+ tenant_id); there is no
        # separate content_hash column.
        blob = (
            db.query(ContentBlob)
            .filter(ContentBlob.id == content_hash,
                    ContentBlob.tenant_id == tenant_id)
            .first()
        )
        if blob is not None:
            hit_ext = blob.file_extension or ""
            hit_size = blob.size_bytes or hit_size
    return hit_name, hit_ext, hit_size


def get_existing_cache_output_row(db: Session, content_hash: str, node_run_id):
    """`_maybe_cache_short_circuit` — dedup check before inserting the
    synthetic `WorkflowNodeOutput` row for a cache hit (same content_hash +
    node_run_id already recorded)."""
    from models import WorkflowNodeOutput

    return db.query(WorkflowNodeOutput).filter(
        WorkflowNodeOutput.content_hash == content_hash,
        WorkflowNodeOutput.node_run_id == node_run_id,
    ).first()
