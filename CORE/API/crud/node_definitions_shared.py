"""Data-access layer for `routers/node_definitions_domains/_shared.py`
(Gate 2b migration of the SHARED sub-module itself — see `services/_base.py`
for the layering contract this follows).

`_shared.py` hosts no endpoint handlers of its own (Gate 2a's domain split
moved every `@router.*` handler into a per-domain sub-module); it is a pure
helpers/Pydantic-models/constants module that the sibling domain modules
`from ._shared import *`. This crud module is the ONLY place
`db.query(models.X)` / `session.query(models.X)` lives for the handful of
`_shared.py` helper functions that embed inline queries — everything else in
`_shared.py` (pure string/env logic, Pydantic models, admission wrappers) has
no data access and is untouched.

Every fetch here is intentionally caller-UNSCOPED (no `tenant_id` filter
baked in) — the service layer (`services/node_definitions_shared_service.py`)
applies tenant/ownership policy on top, so a legitimate 403 never collapses
into a 404 (SHARED RULES #5). This matters concretely for
`get_node_definition_by_id`, which backs `_resolve_tenant_node_for_admin`'s
404-vs-400 tenant-ownership check.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from sqlalchemy.orm import Session

import models


# ---------------------------------------------------------------------
# Web-search query-expansion (`_run_web_query_expansion`)
# ---------------------------------------------------------------------

def get_system_workflow_by_name(db: Session, name: str) -> Optional["models.Workflow"]:
    """Fetch a system-flagged Workflow by exact name (e.g. the hidden
    "System — Web Search" workflow). Unscoped by tenant — system workflows
    have no owning tenant."""
    return (
        db.query(models.Workflow)
        .filter(
            models.Workflow.name == name,
            models.Workflow.is_system.is_(True),
        )
        .first()
    )


def get_workflow_version_by_id(db: Session, version_id: int) -> Optional["models.WorkflowVersion"]:
    """Fetch a WorkflowVersion by id."""
    return (
        db.query(models.WorkflowVersion)
        .filter(models.WorkflowVersion.id == version_id)
        .first()
    )


def get_latest_workflow_version(db: Session, workflow_id: int) -> Optional["models.WorkflowVersion"]:
    """Fetch the highest `version_number` WorkflowVersion for a workflow."""
    return (
        db.query(models.WorkflowVersion)
        .filter(models.WorkflowVersion.workflow_id == workflow_id)
        .order_by(models.WorkflowVersion.version_number.desc())
        .first()
    )


def get_latest_node_run_for_graph_node(
    db: Session, run_id: int, graph_node_id: str,
) -> Optional["models.NodeRun"]:
    """Fetch the most recent NodeRun for a specific graph node within a run,
    joined through WorkflowNode on `graph_node_id`."""
    return (
        db.query(models.NodeRun)
        .join(
            models.WorkflowNode,
            models.NodeRun.workflow_node_id == models.WorkflowNode.id,
        )
        .filter(
            models.NodeRun.workflow_run_id == run_id,
            models.WorkflowNode.graph_node_id == graph_node_id,
        )
        .order_by(models.NodeRun.id.desc())
        .first()
    )


def get_workflow_run_by_id(db: Session, run_id: int) -> Optional["models.WorkflowRun"]:
    """Fetch a WorkflowRun by id."""
    return db.query(models.WorkflowRun).filter(models.WorkflowRun.id == run_id).first()


# ---------------------------------------------------------------------
# Raw-context build submission (`submit_raw_context_build`)
# ---------------------------------------------------------------------

def get_workflow_by_id(db: Session, workflow_id: int) -> Optional["models.Workflow"]:
    """Fetch a Workflow by id. Unscoped — used to re-fetch the just-created
    build workflow for the dispatch step."""
    return db.query(models.Workflow).filter(models.Workflow.id == workflow_id).first()


# ---------------------------------------------------------------------
# NodeDefinition lookups shared by several _shared.py helpers
# ---------------------------------------------------------------------

def get_node_definition_by_id(db: Session, node_definition_id: int) -> Optional["models.NodeDefinition"]:
    """Fetch a NodeDefinition by id, tenant-UNSCOPED.

    Backs `_resolve_tenant_node_for_admin` (404-vs-400 tenant/ownership
    policy applied by the service), `_dispatch_trivy_scan` (status-flip
    fetches) and the Harbor scan-poll helpers. Any `Session` works here,
    including an ad-hoc `SessionLocal()` from a background scan-poll thread.
    """
    return (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.id == node_definition_id)
        .first()
    )


# ---------------------------------------------------------------------
# node-role "use" capability (`_node_definition_use_capability`)
# ---------------------------------------------------------------------

def get_use_capability(db: Session) -> Optional["models.Capability"]:
    """Fetch the seeded `(node_definition, use)` Capability row."""
    return db.query(models.Capability).filter_by(
        resource_type=models.ResourceType.NODE_DEFINITION,
        name="use",
    ).first()


# ---------------------------------------------------------------------
# Federation peer trust aggregation (`_bad_federation_peers`)
# ---------------------------------------------------------------------

def get_federation_peer_aggregates(db: Session) -> List[Any]:
    """Per-`source_peer` success/failure/row-count aggregates over
    `FederatedRemediation`. Rows expose `.source_peer`, `.s` (sum of
    success_count), `.f` (sum of failure_count), `.entries` (row count)."""
    from sqlalchemy import func

    return (
        db.query(
            models.FederatedRemediation.source_peer,
            func.coalesce(func.sum(models.FederatedRemediation.success_count), 0).label("s"),
            func.coalesce(func.sum(models.FederatedRemediation.failure_count), 0).label("f"),
            func.count(models.FederatedRemediation.id).label("entries"),
        )
        .group_by(models.FederatedRemediation.source_peer)
        .all()
    )


# ---------------------------------------------------------------------
# Billing-principal resolution (`_effective_billing_user` / `_effective_creator_user_id`)
# ---------------------------------------------------------------------

def get_build_spec_record(db: Session, build_spec_id: int) -> Optional["models.BuildSpecRecord"]:
    """Fetch a BuildSpecRecord by id. Unscoped."""
    return (
        db.query(models.BuildSpecRecord)
        .filter(models.BuildSpecRecord.id == int(build_spec_id))
        .first()
    )


def get_user_by_id(db: Session, user_id: int) -> Optional["models.User"]:
    """Fetch a User by id. Unscoped."""
    return db.query(models.User).filter(models.User.id == user_id).first()


# ---------------------------------------------------------------------
# Tenant-image soft-delete auto-clear (`_clear_soft_deletes_for_node_refs`)
# ---------------------------------------------------------------------

def find_active_tenant_image_deletion(
    db: Session, tenant_id: int, repository: str, tag: str,
) -> Optional["models.TenantImageDeletion"]:
    """Fetch an un-reaped, active soft-delete row for a (tenant, repo, tag)
    triple. `reaped_at.is_(None)` is the ISO append-only soft-delete filter —
    ported 1:1, unchanged."""
    return db.query(models.TenantImageDeletion).filter(
        models.TenantImageDeletion.tenant_id == tenant_id,
        models.TenantImageDeletion.repository == repository,
        models.TenantImageDeletion.tag == tag,
        models.TenantImageDeletion.reaped_at.is_(None),
    ).first()


# ---------------------------------------------------------------------
# Auto-calibration tenant fallback (`_auto_calibrate_new_node_hook`)
# ---------------------------------------------------------------------

def get_fallback_tenant_id(db: Session) -> Optional[int]:
    """Lowest-id Tenant, used as a last-resort dispatch tenant when neither
    the node's effective tenant nor the creating user's tenant resolves."""
    row: Optional[Tuple[int]] = (
        db.query(models.Tenant.id).order_by(models.Tenant.id).first()
    )
    return row[0] if row else None
