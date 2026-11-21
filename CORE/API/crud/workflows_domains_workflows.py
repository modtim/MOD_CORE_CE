"""CRUD (query layer only) for routers/workflows_domains/workflows.py — Gate 2b.

Covers the raw `db.query(...)` sites that were still inline in the two
heavy/stateful handlers of that router (`trigger_workflow_run`,
`node_run_action`). Everything else in that router already reads through
`crud.workflows_domain` (an earlier wave's shared crud module for this same
file) or the top-level `crud` module — this file adds the remaining sites
rather than duplicating what's already there.

Pure data access only: no auth/policy decisions, no mutation of business
state, no commits. Filters are ported 1:1 from the original inline queries —
including where a query is intentionally NOT tenant-scoped (the caller does
the tenant check after fetch) — so behavior is unchanged.
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

from sqlalchemy.orm import Session

import models


# =============================================================================
# trigger_workflow_run
# =============================================================================

def get_secret_by_name(db: Session, name: str, tenant_id: int) -> Optional[models.Secret]:
    return db.query(models.Secret).filter(
        models.Secret.name == name,
        models.Secret.tenant_id == tenant_id,
        models.Secret.deleted_at.is_(None),
    ).first()


def get_cog_any_tenant(db: Session, cog_id: int) -> Optional[models.Cog]:
    """Fetch by id + soft-delete filter only — NO tenant filter in the query.
    Matches the original inline queries in trigger_workflow_run, which check
    `_cog.tenant_id != current_user.tenant_id` AFTER the fetch (so a
    cross-tenant id resolves to a row but still fails the caller's check,
    rather than silently 404-ing at the query level). Do not "fix" this by
    adding a tenant filter here — it would change which branch raises."""
    return db.query(models.Cog).filter(
        models.Cog.id == cog_id,
        models.Cog.deleted_at.is_(None),
    ).first()


def get_active_run_for_dedup(
    db: Session, version_id: int, user_id: int, purpose: str
) -> Optional[models.WorkflowRun]:
    return db.query(models.WorkflowRun).filter(
        models.WorkflowRun.workflow_version_id == version_id,
        models.WorkflowRun.triggered_by_user_id == user_id,
        models.WorkflowRun.status.in_([models.TaskStatus.QUEUED, models.TaskStatus.RUNNING]),
        models.WorkflowRun.purpose == purpose,
    ).first()


def get_file_by_cog_id(db: Session, cog_id: int, tenant_id: int) -> Optional[models.File]:
    return db.query(models.File).filter(
        models.File.cog_id == cog_id,
        models.File.tenant_id == tenant_id,
        models.File.deleted_at.is_(None),
    ).first()


# =============================================================================
# node_run_action
# =============================================================================

def get_tasks_by_node_and_statuses(
    db: Session, run_id: int, node_id: str, statuses: Sequence
) -> List[models.ApiTaskQueue]:
    return db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.workflow_run_id == run_id,
        models.ApiTaskQueue.node_id == node_id,
        models.ApiTaskQueue.status.in_(statuses),
    ).all()


def get_latest_real_task_for_node(
    db: Session, run_id: int, node_id: str
) -> Optional[models.ApiTaskQueue]:
    """Most recent non-synthetic (queue_name != "node_action") task for a
    node — used both to build the clear_cache lookup key and as the "prior
    real attempt" source for rerun_same_data/rerun_with_downstream."""
    return (
        db.query(models.ApiTaskQueue)
        .filter(
            models.ApiTaskQueue.workflow_run_id == run_id,
            models.ApiTaskQueue.node_id == node_id,
            models.ApiTaskQueue.queue_name != "node_action",
        )
        .order_by(models.ApiTaskQueue.id.desc())
        .first()
    )


def get_latest_completed_task_for_node(
    db: Session, run_id: int, node_id: str
) -> Optional[models.ApiTaskQueue]:
    """Most recent COMPLETED real task for a parent node — used to resolve a
    to-be-rerun node's parent outputs when it has no prior real execution of
    its own."""
    return (
        db.query(models.ApiTaskQueue)
        .filter(
            models.ApiTaskQueue.workflow_run_id == run_id,
            models.ApiTaskQueue.node_id == node_id,
            models.ApiTaskQueue.queue_name != "node_action",
            models.ApiTaskQueue.status == models.ApiTaskStatus.COMPLETED,
        )
        .order_by(models.ApiTaskQueue.id.desc())
        .first()
    )


def get_node_definition_by_kind(db: Session, node_kind: Optional[str]) -> Optional[models.NodeDefinition]:
    return db.query(models.NodeDefinition).filter(
        models.NodeDefinition.node_kind == node_kind
    ).first()


def count_tasks_for_node(db: Session, run_id: int, node_id: str) -> int:
    return db.query(models.ApiTaskQueue).filter(
        models.ApiTaskQueue.workflow_run_id == run_id,
        models.ApiTaskQueue.node_id == node_id,
    ).count()


def get_task_by_id(db: Session, task_id: int) -> Optional[models.ApiTaskQueue]:
    return db.query(models.ApiTaskQueue).get(task_id)


def get_cache_rows_by_key(
    db: Session, image_digest: str, input_content_hash: str, config_hash: str, tenant_id: int
) -> List[Any]:
    """Row set of `(id,)` tuples matching the exact content-addressed cache
    key — the row a future `_maybe_cache_short_circuit` would hit."""
    return db.query(models.NodeOutputCache.id).filter(
        models.NodeOutputCache.image_digest == image_digest,
        models.NodeOutputCache.input_content_hash == input_content_hash,
        models.NodeOutputCache.config_hash == config_hash,
        models.NodeOutputCache.tenant_id == tenant_id,
    ).all()


def get_cache_rows_by_output_hashes(
    db: Session, tenant_id: int, output_hashes: Sequence[str]
) -> List[Any]:
    """Row set of `(id,)` tuples for cache entries resolving to any of the
    given output content hashes — catches sibling cache rows."""
    return db.query(models.NodeOutputCache.id).filter(
        models.NodeOutputCache.tenant_id == tenant_id,
        models.NodeOutputCache.output_content_hash.in_(output_hashes),
    ).all()


def delete_cache_rows(db: Session, ids: Sequence) -> int:
    """Evict NodeOutputCache INDEX rows only (no blob touched — ISO audit
    trail on the underlying content is untouched). Not a soft-delete target:
    NodeOutputCache is a cache index, not user/audit data."""
    if not ids:
        return 0
    # ISO-exception: NodeOutputCache is a cache index (no blob touched, not user/audit data); hard-delete required
    return db.query(models.NodeOutputCache).filter(
        models.NodeOutputCache.id.in_(ids),
    ).delete(synchronize_session=False)
