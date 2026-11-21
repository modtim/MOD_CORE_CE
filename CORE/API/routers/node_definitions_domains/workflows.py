"""node_definitions endpoints for the `workflows` domain.

Split out of the monolithic routers/node_definitions.py during Gate 2a
(openspec: stack-training-and-prewarm). Pure behaviour-preserving MOVE:
paths, methods, dependencies, request/response models and helpers are
unchanged. The endpoints below attach to this sub-module's `router`
(and `external_router`), which the aggregator includes at the same
mount points main.py already uses.

Gate 2b migration: data access moved to
`crud/node_definitions_workflows.py` (the only place `db.query` runs
for this sub-module) and business logic moved to
`services/node_definitions_workflows_service.py`. Handlers below stay
thin HTTP shims — unchanged signature/docstring/decorator/tags — mapping a
service-layer `ServiceError` back to the exact `HTTPException` shape
(status code + detail body) the pre-migration inline code raised.
`analyze_repo` never touched the DB pre-migration and still doesn't; it
returns a plain dict and relies on the endpoint's `response_model` to
validate/serialize it, same as the pre-migration handler's
`AnalyzeScriptResponse(**...)` construction.
"""

from fastapi import APIRouter
from ._shared import *  # noqa: F401,F403  (helpers, models, imports, constants)

import services.node_definitions_workflows_service as service
from services._base import ServiceError

router = APIRouter(
    responses={404: {"description": "Not found"}},
)

_STATUS_BY_CODE = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "bad_request": status.HTTP_400_BAD_REQUEST,
    "payment_required": status.HTTP_402_PAYMENT_REQUIRED,
    "payload_too_large": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    "bad_gateway": status.HTTP_502_BAD_GATEWAY,
}


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer error to the exact `HTTPException` shape the
    pre-migration inline code raised. `detail` reuses `exc.details` when the
    service attached one so the response body shape — dict or plain string —
    is preserved exactly."""
    raise HTTPException(
        status_code=_STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.details if exc.details is not None else exc.message,
    )


def analyze_repo(
    request: AnalyzeRepoRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Same response shape as /analyze-script. Returns a low-confidence
    fallback (with a clear reason) on every failure mode rather than
    raising — the wizard treats this endpoint as advisory, not gating."""
    return service.analyze_repo(request=request, current_user=current_user)

def rebuild_variant_with_edits(
    request: RebuildVariantRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """Rebuild one already-built variant with the operator's edited source.

    The wizard Preview lets an operator edit a non-hero variant's source files
    after the automatic build has run. This endpoint takes those edits, stores
    them on the BuildSpec as an explicit `source_overrides_by_variant[variant]`
    channel (distinct from the Containerizer's generated source — see
    BuildSpec), and re-dispatches a minimal single-variant DAG (build → push →
    sample-test, no LLM regen, no re-register). build_one_variant applies the
    override over the staged source for that one variant; the per-variant
    sample-test updates the Node's verification state in place, exactly as it
    does during the full build.

    Ownership: the run must belong to the caller's tenant and have been
    triggered by the caller — the same gate /retry-with-corrections uses.
    """
    try:
        return service.rebuild_variant_with_edits(db, request=request, current_user=current_user)
    except ServiceError as exc:
        _raise_http(exc)

def build_and_register_local(
    request: BuildLocalRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """
    Build the three user-approved Albedo-pattern files and push to the LOCAL
    registry (registry:5000), then register the resulting image as a
    NodeDefinition. Enqueues an imagebuilder task with REGISTRY_TARGET=local;
    the worker writes the three files to its build context, runs docker build,
    and pushes to {LOCAL_REGISTRY}/tenant-{id}/{image_name}:latest.
    """
    try:
        return service.build_and_register_local(db, request=request, current_user=current_user)
    except ServiceError as exc:
        _raise_http(exc)

__all__ = [
    'analyze_repo',
    'rebuild_variant_with_edits',
    'build_and_register_local',
]
