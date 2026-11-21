"""node_definitions endpoints for the `node_marketplace` domain.

Split out of the monolithic routers/node_definitions.py during Gate 2a
(openspec: stack-training-and-prewarm). Pure behaviour-preserving MOVE:
paths, methods, dependencies, request/response models and helpers are
unchanged. The endpoints below attach to this sub-module's `router`
(and `external_router`), which the aggregator includes at the same
mount points main.py already uses.
"""

from fastapi import APIRouter
from ._shared import *  # noqa: F401,F403  (helpers, models, imports, constants)

# Gate 2b: data access -> crud/node_marketplace.py, policy -> the service.
# The handlers below are thin: auth + translate the service's domain errors.
from services._base import ServiceError
from services.node_marketplace_service import node_marketplace_service

#: Domain error `code` -> HTTP status. The service is HTTP-agnostic (raises
#: ServiceError subclasses); the router maps them here, preserving the exact
#: status codes the pre-migration inline handlers returned.
_STATUS_FOR_CODE = {"not_found": status.HTTP_404_NOT_FOUND, "forbidden": status.HTTP_403_FORBIDDEN}

external_router = APIRouter(
    responses={404: {"description": "Not found"}},
)


@external_router.get("/contract")
def get_external_node_contract(
    user: models.User = Depends(require_node_authoring_key),
):
    """The versioned node-contract reference. Backs the MCP `get_node_contract`
    tool so an agent can author a conformant node without platform source."""
    # Pure static-doc I/O, no DB/policy. Routed through the service (which also
    # fixes a Gate-2a path regression in the old `_shared._load_node_contract`).
    return node_marketplace_service.get_node_contract()

@external_router.get("/builds/{task_id}")
def get_external_build_status(
    task_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_node_authoring_key),
):
    """Tenant-scoped build/task status poll for the external authoring path.

    Backs the MCP `get_build_status` tool. Unlike the generic worker status
    endpoint, this enforces that the task belongs to the caller's tenant so it
    cannot be used to read another tenant's build state.
    """
    try:
        return node_marketplace_service.get_build_status(
            db, task_id=task_id, tenant_id=user.tenant_id
        )
    except ServiceError as exc:
        raise HTTPException(
            status_code=_STATUS_FOR_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
            detail=exc.message,
        )

__all__ = [
    'get_external_node_contract',
    'get_external_build_status',
]
