"""Service layer for the WebSearch node result-return callback
(routers/web_search_result.py).

Owns policy (run_task token tenant/run scoping, artifact-shape ->
NodeRun.outputs persistence, logging) and delegates the two `db.query` sites
this router used to run inline (workflow-run lookup + NodeRun resolution) to
`crud/web_search_result.py`.

Follows the `services/_base.py` convention: raises `NotFoundError` (a
`ServiceError` subclass) for the "no such run / no such NodeRun" cases; the
router maps those -> `HTTPException(404, ...)`.

The 403 cross-tenant guard is `pin_run_task_tenant` (utils/run_task_auth.py),
which already raises `HTTPException(403, ...)` directly — same precedent as
`services/node_runs_assets_cogs_files_service.py` /
`services/cogs_assets_cogs_files_service.py`. It is allowed to propagate
through this service unchanged; FastAPI handles an `HTTPException` raised
anywhere in the call stack the same way. (A `models.User` principal —
internal service auth — is already trusted and carries no run_task claims,
so pinning applies only to the token/dict path.)
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy.orm import Session

import models
import crud.web_search_result as crud
from services._base import NotFoundError

logger = logging.getLogger(__name__)


def store_result(
    db: Session,
    *,
    workflow_run_id: int,
    node_id: str,
    artifact: Dict[str, Any],
    principal: Any,
) -> models.NodeRun:
    """Persist the WebSearch container's OUTPUT artifact onto the NodeRun.

    Resolves the run, enforces the run_task token's tenant+run scope, then
    stores the artifact on `NodeRun.outputs` so the orchestrator / node
    wizard reads it back through the standard node-output resolution.
    """
    from utils.run_task_auth import pin_run_task_tenant

    run = crud.get_workflow_run(db, workflow_run_id)
    if run is None:
        raise NotFoundError(f"No workflow run {workflow_run_id}")

    if isinstance(principal, dict):
        pin_run_task_tenant(principal, tenant_id=run.tenant_id, run_id=workflow_run_id)

    node_run = crud.get_latest_node_run(db, workflow_run_id, node_id)
    if node_run is None:
        logger.warning(
            "web-search-result: no NodeRun for run=%s node_id=%s",
            workflow_run_id, node_id,
        )
        raise NotFoundError(
            f"No NodeRun found for workflow_run_id={workflow_run_id} node_id={node_id}"
        )

    node_run = crud.save_artifact(db, node_run, artifact)

    logger.info(
        "web-search-result: stored artifact on node_run=%d "
        "(run=%s node_id=%s, %d result(s), ok=%s)",
        node_run.id, workflow_run_id, node_id,
        len(artifact.get("results") or []), artifact.get("ok"),
    )
    return node_run
