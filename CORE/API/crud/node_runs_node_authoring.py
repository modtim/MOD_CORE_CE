# API/crud/node_runs_node_authoring.py
"""Data access for the `node_runs_domains.node_authoring` sub-router
(routers/node_runs_domains/node_authoring.py -- GET /node-runs/{run_id}).

The ONLY place `db.query(models.NodeRun)` runs for this domain (Gate 2b
migration). Distinct from `crud/node_run_cud.py` (a different, older CRUD
module backing other node-run call sites, e.g. `get_node_runs_for_workflow_run`
there has a plain, un-eager-loaded signature) and from `crud/node_authoring.py`
(a DIFFERENT domain entirely -- `routers/node_definitions_domains/node_authoring.py`,
the node-build/marketplace authoring flow). Same base name, different domains;
kept in its own module per the router-package split so each sub-router owns
its own crud/service pair.

Read is intentionally NOT tenant-scoped here (ported 1:1) -- the pre-migration
inline query filtered only by `workflow_run_id`; tenant + per-workflow-view
policy is enforced in the service layer (see the 404-vs-403 note in
`services/_base.py`), which fetches the parent `WorkflowRun` tenant-scoped
first via the existing `crud.get_workflow_run_by_id`.
"""
from __future__ import annotations

from typing import List

from sqlalchemy.orm import Session, joinedload

import models


def get_node_runs_with_workflow_node(db: Session, run_id: int) -> List[models.NodeRun]:
    """All `NodeRun` rows for `run_id`, eager-loading `workflow_node` (needed
    for `graph_node_id` in the UI's status updates)."""
    return db.query(models.NodeRun).options(
        joinedload(models.NodeRun.workflow_node)
    ).filter(
        models.NodeRun.workflow_run_id == run_id
    ).all()
