"""Data-access layer for the CE-eligible slice of the `node_authoring`
domain (openspec: stack-training-and-prewarm, Gate 2 / D9; CE-launch Gate 4:
"CE fanout; marketplace/team-sharing business logic is the non-CE slice").

The ONLY place `db.query(models.X)` lives for the CE endpoints that live in
`routers/node_definitions_domains/node_authoring.py` (build-spec/variant
lifecycle, remediation/federation/built-image caches, and core NodeDefinition
CRUD). Extracted verbatim (same filters/order/limits as the original inline
router queries) during the Gate-2 per-domain migration — behaviour-preserving
MOVE, not a rewrite.

PAID marketplace / tenant-team-sharing data access (curate lists, marketplace
visibility, TenantNodeEntitlement, team ResourcePermission grants) lives in
the sibling `crud/node_marketplace_paid.py` instead — CE does not ship it.

Companion service layer: `services/node_authoring_service.py` (CE) and
`services/node_marketplace_paid.py` (paid).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

import models


# =============================================================================
# BuildSpecRecord
# =============================================================================


def get_build_spec_by_id(db: Session, spec_id: int) -> Optional["models.BuildSpecRecord"]:
    return (
        db.query(models.BuildSpecRecord)
        .filter(models.BuildSpecRecord.id == spec_id)
        .first()
    )


# =============================================================================
# WorkflowRun / WorkflowRunEvent (read-only, used by build_spec_eta)
# =============================================================================


def get_workflow_run_by_id(db: Session, workflow_run_id: int) -> Optional["models.WorkflowRun"]:
    return (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.id == workflow_run_id)
        .first()
    )


def get_last_workflow_run_event(db: Session, workflow_run_id: int) -> Optional["models.WorkflowRunEvent"]:
    return (
        db.query(models.WorkflowRunEvent)
        .filter(models.WorkflowRunEvent.workflow_run_id == workflow_run_id)
        .order_by(models.WorkflowRunEvent.id.desc())
        .first()
    )


# =============================================================================
# NodeVariantVerification
# =============================================================================


def get_verification_by_spec_and_variant(
    db: Session, build_spec_id: int, variant: str,
) -> Optional["models.NodeVariantVerification"]:
    return (
        db.query(models.NodeVariantVerification)
        .filter(
            models.NodeVariantVerification.build_spec_id == build_spec_id,
            models.NodeVariantVerification.variant == variant,
        )
        .first()
    )


def get_verification_by_node_and_variant(
    db: Session, node_definition_id: int, variant: str,
) -> Optional["models.NodeVariantVerification"]:
    return (
        db.query(models.NodeVariantVerification)
        .filter(
            models.NodeVariantVerification.node_definition_id == node_definition_id,
            models.NodeVariantVerification.variant == variant,
        )
        .first()
    )


def list_verifications_for_node(
    db: Session, node_definition_id: int,
) -> List["models.NodeVariantVerification"]:
    return (
        db.query(models.NodeVariantVerification)
        .filter(models.NodeVariantVerification.node_definition_id == node_definition_id)
        .order_by(models.NodeVariantVerification.variant)
        .all()
    )


def list_verifications_for_spec(
    db: Session, build_spec_id: int,
) -> List["models.NodeVariantVerification"]:
    return (
        db.query(models.NodeVariantVerification)
        .filter(models.NodeVariantVerification.build_spec_id == build_spec_id)
        .all()
    )


def list_unfinalized_verifications_for_spec(
    db: Session, build_spec_id: int,
) -> List["models.NodeVariantVerification"]:
    return (
        db.query(models.NodeVariantVerification)
        .filter(
            models.NodeVariantVerification.build_spec_id == build_spec_id,
            models.NodeVariantVerification.node_definition_id.is_(None),
        )
        .all()
    )


# =============================================================================
# NodeDefinition
# =============================================================================


def get_node_definition_unscoped(db: Session, node_definition_id: int) -> Optional["models.NodeDefinition"]:
    """Fetch by id with NO tenant filter. Callers apply their own tenant/
    ownership policy in the service layer (404-vs-403 trap — see SHARED
    RULES: keep the fetch tenant-un-scoped, apply policy above it)."""
    return (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.id == node_definition_id)
        .first()
    )


def get_system_node_definition_by_name(db: Session, name: str) -> Optional["models.NodeDefinition"]:
    return (
        db.query(models.NodeDefinition)
        .filter(
            models.NodeDefinition.name == name,
            models.NodeDefinition.tenant_id.is_(None),
        )
        .first()
    )


def find_node_by_name_scoped(
    db: Session, node_name: str, tenant_id: Optional[int],
) -> Optional["models.NodeDefinition"]:
    q = db.query(models.NodeDefinition).filter(models.NodeDefinition.name == node_name)
    if tenant_id is not None:
        q = q.filter(models.NodeDefinition.tenant_id == tenant_id)
    return q.first()


# NOTE: list_curated_system_nodes / list_tenant_nodes /
# list_marketplace_system_nodes / list_marketplace_tenant_nodes moved to
# crud/node_marketplace_paid.py (PAID — CE-launch Gate 4).


def list_nodes_with_custom_metadata(db: Session) -> List["models.NodeDefinition"]:
    return (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.custom_metadata != None)  # noqa: E711
        .all()
    )


# NOTE: marketplace curate-list / marketplace-visibility queries, tenant
# TenantNodeEntitlement queries, and tenant-team ResourcePermission-grant
# queries moved to crud/node_marketplace_paid.py (PAID — CE-launch Gate 4).


# =============================================================================
# Tenant
# =============================================================================


def get_tenant_by_id(db: Session, tenant_id: int) -> Optional["models.Tenant"]:
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()


# =============================================================================
# BuildRemediation / FederatedRemediation / BuiltImageCache
# =============================================================================


def list_remediations_by_signature(db: Session, signature: str) -> List["models.BuildRemediation"]:
    return (
        db.query(models.BuildRemediation)
        .filter(models.BuildRemediation.error_signature == signature)
        .all()
    )


def get_remediation_by_id(db: Session, remediation_id: int) -> Optional["models.BuildRemediation"]:
    return (
        db.query(models.BuildRemediation)
        .filter(models.BuildRemediation.id == remediation_id)
        .first()
    )


def get_remediation_by_signature_and_diff(
    db: Session, signature: str, diff_hash: str,
) -> Optional["models.BuildRemediation"]:
    return (
        db.query(models.BuildRemediation)
        .filter(
            models.BuildRemediation.error_signature == signature,
            models.BuildRemediation.diff_hash == diff_hash,
        )
        .first()
    )


def get_most_recently_used_remediation(db: Session) -> Optional["models.BuildRemediation"]:
    return (
        db.query(models.BuildRemediation)
        .order_by(models.BuildRemediation.last_used_at.desc())
        .first()
    )


def list_promoted_remediations(db: Session, limit: int = 200) -> List["models.BuildRemediation"]:
    return (
        db.query(models.BuildRemediation)
        .filter(models.BuildRemediation.promoted_at.isnot(None))
        .order_by(models.BuildRemediation.promoted_at.desc())
        .limit(limit)
        .all()
    )


def list_top_promoted_remediations_by_success(db: Session, limit: int = 10) -> List["models.BuildRemediation"]:
    return (
        db.query(models.BuildRemediation)
        .filter(models.BuildRemediation.promoted_at.isnot(None))
        .order_by(models.BuildRemediation.success_count.desc())
        .limit(limit)
        .all()
    )


def remediation_cache_aggregate_stats(db: Session, cutoff_24h, cutoff_7d) -> Dict[str, Any]:
    """Aggregate counters for the admin remediation-stats dashboard.
    One place for the six scalar aggregate queries the endpoint needs."""
    total = db.query(func.count(models.BuildRemediation.id)).scalar() or 0
    promoted = (
        db.query(func.count(models.BuildRemediation.id))
        .filter(models.BuildRemediation.promoted_at.isnot(None))
        .scalar()
        or 0
    )
    sum_success = (
        db.query(func.coalesce(func.sum(models.BuildRemediation.success_count), 0)).scalar() or 0
    )
    sum_failure = (
        db.query(func.coalesce(func.sum(models.BuildRemediation.failure_count), 0)).scalar() or 0
    )
    used_24h = (
        db.query(func.count(models.BuildRemediation.id))
        .filter(models.BuildRemediation.last_used_at >= cutoff_24h)
        .scalar()
        or 0
    )
    promoted_7d = (
        db.query(func.count(models.BuildRemediation.id))
        .filter(models.BuildRemediation.promoted_at >= cutoff_7d)
        .scalar()
        or 0
    )
    return {
        "total_remediations": total,
        "promoted": promoted,
        "sum_success": sum_success,
        "sum_failure": sum_failure,
        "used_24h": used_24h,
        "promoted_7d": promoted_7d,
    }


def built_image_cache_total_and_hits(db: Session) -> Tuple[int, int]:
    total = db.query(func.count(models.BuiltImageCache.id)).scalar() or 0
    total_hits = (
        db.query(func.coalesce(func.sum(models.BuiltImageCache.hit_count), 0)).scalar() or 0
    )
    return total, int(total_hits)


def list_federated_by_signature(db: Session, signature: str) -> List["models.FederatedRemediation"]:
    return (
        db.query(models.FederatedRemediation)
        .filter(models.FederatedRemediation.error_signature == signature)
        .all()
    )


def get_federated_by_signature_diff_peer(
    db: Session, signature: str, diff_hash: str, source_peer: str,
) -> Optional["models.FederatedRemediation"]:
    return (
        db.query(models.FederatedRemediation)
        .filter(
            models.FederatedRemediation.error_signature == signature,
            models.FederatedRemediation.diff_hash == diff_hash,
            models.FederatedRemediation.source_peer == source_peer,
        )
        .first()
    )


def federated_remediation_total(db: Session) -> int:
    return db.query(func.count(models.FederatedRemediation.id)).scalar() or 0


def federated_remediation_peer_aggregates(db: Session):
    return (
        db.query(
            models.FederatedRemediation.source_peer,
            func.count(models.FederatedRemediation.id).label("entries"),
            func.max(models.FederatedRemediation.last_seen_at).label("last_seen_at"),
        )
        .group_by(models.FederatedRemediation.source_peer)
        .all()
    )


def get_built_image_cache_entry(
    db: Session, *, source_hash: str, image_name: str, variant: str,
) -> Optional["models.BuiltImageCache"]:
    return (
        db.query(models.BuiltImageCache)
        .filter(
            models.BuiltImageCache.source_hash == source_hash,
            models.BuiltImageCache.image_name == image_name,
            models.BuiltImageCache.variant == variant,
        )
        .first()
    )


# =============================================================================
# ApiTaskQueue
# =============================================================================


def get_api_task_by_id(db: Session, task_id: int) -> Optional["models.ApiTaskQueue"]:
    return (
        db.query(models.ApiTaskQueue)
        .filter(models.ApiTaskQueue.id == task_id)
        .first()
    )


def count_in_flight_scan_tasks(db: Session, node_kind_expr) -> int:
    return (
        db.query(models.ApiTaskQueue)
        .filter(
            node_kind_expr == "trivy_scan",
            models.ApiTaskQueue.status.in_(
                [models.ApiTaskStatus.RUNNING, models.ApiTaskStatus.CLAIMED],
            ),
        )
        .count()
    )


def list_pending_scan_tasks(db: Session, node_kind_expr) -> List["models.ApiTaskQueue"]:
    return (
        db.query(models.ApiTaskQueue)
        .filter(
            node_kind_expr == "trivy_scan",
            models.ApiTaskQueue.status == models.ApiTaskStatus.PENDING,
        )
        .order_by(models.ApiTaskQueue.priority.desc(), models.ApiTaskQueue.created_at.asc())
        .all()
    )


def get_last_scan_task_for_node(
    db: Session, node_kind_expr, node_definition_id: int,
) -> Optional["models.ApiTaskQueue"]:
    return (
        db.query(models.ApiTaskQueue)
        .filter(
            node_kind_expr == "trivy_scan",
            models.ApiTaskQueue.task_payload.op("->")("container_env").op("->>")(
                "NODE_DEFINITION_ID",
            )
            == str(node_definition_id),
            models.ApiTaskQueue.status.in_(
                [models.ApiTaskStatus.COMPLETED, models.ApiTaskStatus.FAILED],
            ),
        )
        .order_by(models.ApiTaskQueue.id.desc())
        .first()
    )
