"""node_definitions endpoints for the `node_wizard` domain.

Split out of the monolithic routers/node_definitions.py during Gate 2a
(openspec: stack-training-and-prewarm). Pure behaviour-preserving MOVE:
paths, methods, dependencies, request/response models and helpers are
unchanged. The endpoints below attach to this sub-module's `router`
(and `external_router`), which the aggregator includes at the same
mount points main.py already uses.

Gate 2b migration: data access moved to
`crud/node_definitions_node_wizard.py` (the only place `db.query` runs
for this sub-module) and business logic moved to
`services/node_definitions_node_wizard_service.py`. Handlers below stay
thin HTTP shims — unchanged signature/docstring/decorator/tags — mapping a
service-layer `ServiceError` back to the exact `HTTPException` shape
(status code + detail body) the pre-migration inline code raised.
"""

from fastapi import APIRouter
from ._shared import *  # noqa: F401,F403  (helpers, models, imports, constants)

import services.node_definitions_node_wizard_service as service
from services._base import ServiceError

router = APIRouter(
    responses={404: {"description": "Not found"}},
)
external_router = APIRouter(
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
    service attached one (e.g. a collision-detail dict) so the response body
    shape — dict or plain string — is preserved exactly."""
    raise HTTPException(
        status_code=_STATUS_BY_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.details if exc.details is not None else exc.message,
    )


def build_and_register_node(
    request: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Unified entry point for the wizard's Create Node button.

    Accepts two request shapes:

    1. **Inputs-only** (wizard-as-workflow-native, preferred):
       caller sends `node_name`, one of {`source_repo_url`,
       `inline_source_body`, `wrap_base_image`}, plus the usual
       metadata (input_type, output_type, process_type, etc.). The
       BuildSpec is persisted with empty Dockerfile/scripts, and the
       workflow_run starts with the `system-wizard-generate` DAG
       node which calls the Containerizer LLM. The next node
       (`system-wizard-ingest-artifacts`) merges artifacts onto the
       BuildSpec; then the variant build/push/register nodes proceed.
       One workflow_run, one progress endpoint, fully driven by
       the orchestrator.

    2. **Pre-generated artifacts** (legacy /build-and-register-local
       compat): caller sends `dockerfile`, `entrypoint_sh`,
       `build_and_push_sh`, optional `dockerfile_variants` — same
       shape the wizard sent before the workflow-native refactor.
       Used by retry-with-corrections and any non-wizard caller
       that already has artifacts in hand.

    Detection: if `dockerfile` is present in the request body, mode
    is legacy. Otherwise mode is inputs-only and at least one of the
    three input slots must be set (400 if not).
    """
    try:
        return service.build_and_register_node(db, request=request, current_user=current_user)
    except ServiceError as exc:
        _raise_http(exc)

def get_build_run_progress(
    workflow_run_id: int,
    current_user: models.User = Depends(require_active_and_onboarded_user),
    db: Session = Depends(get_db),
):
    """δ — progress poll for a build dispatched via /build-and-register-node.

    The DAG-based phase-2 dispatch returns a `workflow_run_id`, not an
    `ApiTaskQueue.id`. The legacy /generation-artifacts/{task_id} endpoint
    only queries `ApiTaskQueue`, which means a poll keyed on a
    workflow_run_id either 404s OR collides with an unrelated old task
    that happens to share that integer (recently observed: workflow_run
    id=4 reading the log_tail from a 4-day-old completed task #4). This
    endpoint is the workflow_run-aware counterpart.

    Response shape mirrors /generation-artifacts so the wizard's
    BuildProgressCard can switch endpoints based on phase without
    rewriting its consumer. Status maps WorkflowRun.status:
      queued/pending     → "pending"
      running/blocked    → "running"
      success/completed  → "completed"
      failure/failed     → "failed"

    log_tail is aggregated across the child api_task_queue rows that
    belong to this workflow_run, oldest-first. Each task's contribution
    is prefixed with its `[task <id> (<variant>) status=<status>]`
    header so the BuildProgressCard's stage inference can find the
    canonical hint strings and so a failing variant is identifiable.
    """
    try:
        return service.get_build_run_progress(
            db, workflow_run_id=workflow_run_id, current_user=current_user,
        )
    except ServiceError as exc:
        _raise_http(exc)

@external_router.get("/base-images")
def list_external_base_images(
    user: models.User = Depends(require_node_authoring_key),
):
    """The base-image allowlist an external author must build against.

    Backs the MCP `list_base_images` tool. `mode` reflects whether the
    prefixes are advisory or strictly enforced for the wizard; the external
    authoring path always enforces them regardless.
    """
    return service.list_external_base_images()

__all__ = [
    'build_and_register_node',
    'get_build_run_progress',
    'list_external_base_images',
]
