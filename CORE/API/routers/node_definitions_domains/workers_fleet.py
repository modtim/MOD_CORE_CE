"""node_definitions endpoints for the `workers_fleet` domain.

Split out of the monolithic routers/node_definitions.py during Gate 2a
(openspec: stack-training-and-prewarm). Pure behaviour-preserving MOVE:
paths, methods, dependencies, request/response models and helpers are
unchanged. The endpoints below attach to this sub-module's `router`
(and `external_router`), which the aggregator includes at the same
mount points main.py already uses.

Gate 2b: query-heavy handlers below delegate to crud/services (module-level
import is safe here — neither module imports back into this package at
import time; see services/node_definitions_workers_fleet_service.py
docstring, same convention as the sibling task_queue_scheduler domain).
"""

from fastapi import APIRouter
from ._shared import *  # noqa: F401,F403  (helpers, models, imports, constants)

from services import node_definitions_workers_fleet_service as _ndwf_svc
from services._base import ServiceError as _ServiceError

router = APIRouter(
    responses={404: {"description": "Not found"}},
)


def _raise_http(exc: _ServiceError) -> None:
    """Map a node_definitions_workers_fleet service-layer error to the HTTP
    response the original inline handler used to raise directly (SHARED
    RULES: router maps ServiceError -> HTTPException)."""
    status_code = _ndwf_svc.STATUS_BY_CODE.get(exc.code, 400)
    detail = exc.details if exc.details is not None else exc.message
    raise HTTPException(status_code=status_code, detail=detail)


def get_wizard_cluster_capabilities(
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """Return the accelerator capabilities the calling tenant's worker
    fleet has, plus the highest-perf one for picking sensible defaults.

    The wizard's describe-chat UI calls this on mount to default the
    `process_type` field to the cluster's best available accelerator.
    A user on an AMD-only deployment shouldn't see "CUDA" pre-selected;
    a user on a CPU-only deployment shouldn't see GPU options at all.
    """
    return _ndwf_svc.get_wizard_cluster_capabilities(db, current_user)

def repair_artifacts(
    request: RepairArtifactsRequest,
    _auth: BuildAuthContext = Depends(require_build_task_token),
    db: Session = Depends(get_db),
):
    """Call the wizard LLM to repair a build whose smoke test failed.

    Build-task's `test_and_repair_one_variant` stage calls this between
    inner-loop iterations. The endpoint:
      1. Loads the BuildSpec by id.
      2. Builds a repair prompt with failure context + current files.
      3. Calls the wizard LLM (on-prem; effectively free per call).
      4. Validates the response (path traversal, missing fields).
      5. Mutates the BuildSpec's hero_dockerfile / source_files /
         entrypoint_sh / build_and_push_sh in place, increments the
         appropriate counter, appends to repair_history.
      6. Returns the deltas so the caller can docker cp / rebuild.

    The caller is expected to track its own retry count via
    `attempt_number` so we can short-circuit if the budget is exhausted
    even if the build-task stage forgot to check.
    """
    try:
        return _ndwf_svc.repair_artifacts(db, request=request, auth=_auth)
    except _ServiceError as exc:
        _raise_http(exc)

__all__ = [
    'get_wizard_cluster_capabilities',
    'repair_artifacts',
]
