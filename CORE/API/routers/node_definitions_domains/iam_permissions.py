"""node_definitions endpoints for the `iam_permissions` domain.

Split out of the monolithic routers/node_definitions.py during Gate 2a
(openspec: stack-training-and-prewarm). Pure behaviour-preserving MOVE:
paths, methods, dependencies, request/response models and helpers are
unchanged. The endpoints below attach to this sub-module's `router`
(and `external_router`), which the aggregator includes at the same
mount points main.py already uses.

Gate 2b migration: data access moved to
`crud/node_definitions_iam_permissions.py` (the only place `db.query` runs
for this sub-module) and business logic moved to
`services/node_definitions_iam_permissions_service.py`. Handlers below stay
thin HTTP shims — unchanged signature/docstring/decorator/tags — mapping a
service-layer `ServiceError` back to the exact `HTTPException` shape
(status code + detail body) the pre-migration inline code raised.
"""

from fastapi import APIRouter
from ._shared import *  # noqa: F401,F403  (helpers, models, imports, constants)

import services.node_definitions_iam_permissions_service as service
from services._base import ServiceError

router = APIRouter(
    responses={404: {"description": "Not found"}},
)

_STATUS_BY_CODE = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "conflict": status.HTTP_409_CONFLICT,
}


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer error to the exact `HTTPException` shape the
    pre-migration inline code raised. `detail` reuses `exc.details` when the
    service attached one (e.g. the `retry_cap_reached` dict payload) so the
    response body shape — dict or plain string — is preserved exactly."""
    raise HTTPException(
        status_code=_STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.details if exc.details is not None else exc.message,
    )


def predict_variants(
    request: PredictVariantsRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """Return the GPU variants the wizard WOULD emit for this source,
    without running the LLM or charging credits. The response feeds
    the Review step's variant checklist — the user toggles Skip
    per-variant before clicking Next (which fires /generate-artifacts
    and deducts credits).

    The compute mirrors the Containerizer's runtime path:
      1. detect_compatible_variants(source, hero_from) — import sniff
      2. Intersect with the live worker fleet's advertised hardware tags
         (union of active workers' `hardware_specs.hardware_tags` — see
         `service.predict_variants`; the old `PlatformGPUSettings.hardware_tags`
         admin whitelist is retired, GE.AIC.5)
      3. Keep only variants in VARIANT_BASES (stub-less path)
    """
    return service.predict_variants(db, request=request)

def retry_with_corrections(
    request: RetryWithCorrectionsRequest,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """Dispatch a new Containerizer task that retries the failed one
    with per-variant failure context appended to ADDITIONAL_INSTRUCTIONS.

    Walks the parent_task_id chain (stored in task_payload — no schema
    migration needed) to count retries, capping at 2 from the origin.
    A user who hits the cap should switch to the Preview step's
    Dockerfile editor and fix manually.
    """
    try:
        return service.retry_with_corrections(db, request=request, current_user=current_user)
    except ServiceError as exc:
        _raise_http(exc)

__all__ = [
    'predict_variants',
    'retry_with_corrections',
]
