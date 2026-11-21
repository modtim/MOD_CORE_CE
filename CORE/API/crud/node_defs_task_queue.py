"""Data-access layer for the `task_queue_scheduler` node_definitions domain
(Gate 2b migration of `routers/node_definitions_domains/task_queue_scheduler.py`
— see `services/_base.py` for the layering contract this follows).

The ONLY place `db.query(models.X)` lives for this slice of the
node_definitions domain: BuildSpecRecord lookups (wizard artifact-ingest /
repair-budget reset), the ApiTaskQueue lookup backing the generation-
artifacts + port-diff polls, and the two NodeDefinition lookups used by
the variant-verification sidecar and the tenant-share endpoint.

Every primary-resource fetch here is intentionally caller-UNSCOPED (no
`tenant_id` filter baked into the query) — the service layer
(`services/node_defs_task_queue_service.py`) applies tenant/ownership
policy on top, so a legitimate 403 never collapses into a 404
(SHARED RULES #5). `get_node_definition_by_name_for_tenant` is the one
exception: it mirrors the original inline query's own tenant filter
verbatim because it only ever feeds a best-effort metadata sidecar, never
a 404/403 decision.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

import models


def get_build_spec_record(db: Session, spec_id: int) -> Optional["models.BuildSpecRecord"]:
    """Fetch a BuildSpecRecord by id. Unscoped — callers apply tenant policy."""
    return db.query(models.BuildSpecRecord).filter_by(id=spec_id).first()


def get_api_task_queue_by_id(db: Session, task_id: int) -> Optional["models.ApiTaskQueue"]:
    """Fetch an ApiTaskQueue row by id. Unscoped — callers apply tenant policy."""
    return db.query(models.ApiTaskQueue).filter(models.ApiTaskQueue.id == task_id).first()


def get_node_definition_by_name_for_tenant(
    db: Session, name: str, tenant_id: Optional[int],
) -> Optional["models.NodeDefinition"]:
    """Best-effort NodeDefinition lookup by (name, tenant_id) — mirrors the
    original inline query exactly. Used only to attach optional
    variant-verification metadata; never a 404/403 decision point."""
    return (
        db.query(models.NodeDefinition)
        .filter(
            models.NodeDefinition.name == name,
            models.NodeDefinition.tenant_id == tenant_id,
        )
        .first()
    )


def get_node_definition_by_id(db: Session, node_definition_id: int) -> Optional["models.NodeDefinition"]:
    """Fetch a NodeDefinition by id. Unscoped — callers apply tenant policy."""
    return (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.id == node_definition_id)
        .first()
    )
