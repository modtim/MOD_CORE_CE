# API/services/node_runs_node_authoring_service.py
"""Business logic for the `node_runs_domains.node_authoring` sub-router
(routers/node_runs_domains/node_authoring.py).

Companion to `crud/node_runs_node_authoring.py` (the ONLY place this domain's
`db.query(models.NodeRun)` runs). The router stays HTTP-only: resolve the
current user via FastAPI `Depends()`, call the function here, return the
result.

404-vs-403: the workflow-run fetch (`crud.get_workflow_run_by_id`) is already
tenant-scoped upstream (existing shared crud, not touched by this migration);
the additional per-workflow view gate (`resource_permissions.can_view_workflow_run`)
is a POLICY check, not a query, so it belongs here -- a denied view collapses
to 404 (not 403) by design, matching the pre-migration router body exactly
(hiding a co-tenant's private workflow run's node graph by run id).
"""
from __future__ import annotations

from typing import List

from sqlalchemy.orm import Session

import crud
import models
from crud import node_runs_node_authoring as node_runs_node_authoring_crud
from services._base import NotFoundError


def get_node_runs_for_workflow_run(
    db: Session, run_id: int, current_user: models.User
) -> List[models.NodeRun]:
    """Retrieve all node runs for `run_id`, gated by the caller's tenant +
    per-workflow view permission. Raises `NotFoundError` (router maps -> 404)
    for both "run doesn't exist" and "run exists but caller can't view its
    workflow" -- ported 1:1 (the 404-vs-403 collapse is intentional here, not
    a bug: it hides the existence of a private workflow's node graph)."""
    db_workflow_run = crud.get_workflow_run_by_id(db, run_id=run_id, tenant_id=current_user.tenant_id)
    if not db_workflow_run:
        raise NotFoundError("Workflow run not found")

    # Per-workflow gate: tenant-scoping alone leaked the node graph of a
    # co-tenant's PRIVATE workflow run by run id. Hide (404) a run whose
    # workflow the caller can't view (owner / view-grant / platform admin).
    from resource_permissions import can_view_workflow_run
    if not can_view_workflow_run(db, current_user, db_workflow_run):
        raise NotFoundError("Workflow run not found")

    # GY.V4b — PII/PHI redaction on READ only, for the run-detail response the
    # caller sees. The stored NodeRun rows are never mutated (a copy is built
    # only when something matched), and the off profile returns the rows as
    # queried, so behaviour is byte-identical to today.
    from utils.pii_redaction_surfaces import redact_node_run_view
    return [redact_node_run_view(nr) for nr in
            node_runs_node_authoring_crud.get_node_runs_with_workflow_node(db, run_id)]
