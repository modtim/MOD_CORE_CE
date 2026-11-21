# API/routers/web_search_result.py
"""add-web-search-node — internal result-return endpoint called by the
WebSearch container.

The WebSearch container runs a keyless DuckDuckGo query (reusing the
node_builder/web_expand.py extraction logic) and POSTs its OUTPUT artifact back
here. It authenticates the SAME way every other callback node does (VectorQuery,
ReturnExport, …): NOT with a standing platform key, but with the short-lived,
tenant+run-scoped ``run_task`` token the worker vends it on demand
(``mod_node_auth`` fetches it from the worker's local token endpoint and sends it
as ``X-API-Key``). ``node_or_service_principal`` accepts that token (or the
existing internal service auth), and ``pin_run_task_tenant`` enforces the token's
scope against the addressed run — so a node can only write to ITS OWN run, and
the tenant is taken from the verified token, never the request body. This is why
``system_web_search`` does NOT belong in ``_PLATFORM_SERVICE_KEY_NODE_KINDS``: it
handles user-supplied content and must use a scoped credential, per that set's
own rule.

Artifact shape (also the NodeDefinition.output_schema):
    { "results": [ {"title": str, "url": str, "snippet": str} ],
      "github_repos": [str],        # "owner/repo"
      "pypi_packages": [str],
      "jargon": [str],
      "ok": bool }
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from dependencies import get_db
from services import web_search_result_service
from services._base import NotFoundError, ServiceError
from utils.run_task_auth import node_or_service_principal

# No router-level auth pin: the per-handler `node_or_service_principal` accepts
# the node's run_task token OR internal service auth, and the handler pins the
# run_task scope after resolving the run's tenant.
#
# clean-v2-api (admin dissolve wave): moved from `/admin/web-search/result` to
# `/system/web-search/result`. `router` carries no prefix and the endpoint is
# registered at both the canonical and old paths directly, because the
# WebSearch container (CORE/DockerContainers/WebSearch/entrypoint.py, a
# non-frontend caller with no code change in this wave — its
# DEFAULT_RESULT_ENDPOINT env default still points at the old path) still
# POSTs its result there. Drop the alias once the container image is
# rebuilt/redeployed with RESULT_ENDPOINT repointed (or its default updated).
router = APIRouter(tags=["System - Web Search"])


class WebSearchArtifact(BaseModel):
    """The node's OUTPUT artifact — mirrors NodeDefinition.output_schema."""
    results: list = Field(default_factory=list)
    github_repos: list = Field(default_factory=list)
    pypi_packages: list = Field(default_factory=list)
    jargon: list = Field(default_factory=list)
    ok: bool = False


class WebSearchResultRequest(BaseModel):
    workflow_run_id: int
    # Graph node id (e.g. "process-1") — matched against
    # WorkflowNode.graph_node_id to resolve the NodeRun.
    node_id: str
    artifact: WebSearchArtifact


class WebSearchResultResponse(BaseModel):
    ok: bool
    node_run_id: Optional[int] = None


@router.post("/system/web-search/result", response_model=WebSearchResultResponse)
def return_web_search_result(
    payload: WebSearchResultRequest = Body(...),
    principal=Depends(node_or_service_principal),
    db: Session = Depends(get_db),
):
    """Persist the WebSearch container's OUTPUT artifact onto the NodeRun.

    The container POSTs ``{ workflow_run_id, node_id, artifact }`` here with its
    ``run_task`` token as ``X-API-Key``. We resolve the run, enforce the token's
    tenant+run scope, then store the artifact on ``NodeRun.outputs`` (the same
    column the worker-task reporting path writes) so the orchestrator / node
    wizard reads it back through the standard node-output resolution.
    """
    artifact: Dict[str, Any] = payload.artifact.dict()
    try:
        node_run = web_search_result_service.store_result(
            db,
            workflow_run_id=payload.workflow_run_id,
            node_id=payload.node_id,
            artifact=artifact,
            principal=principal,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)
    except ServiceError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    return WebSearchResultResponse(ok=True, node_run_id=node_run.id)


# clean-v2-api: `/admin/web-search/result` alias removed — the WebSearch
# container now calls `/system/web-search/result` (image rebuilt 2026-09-23).
