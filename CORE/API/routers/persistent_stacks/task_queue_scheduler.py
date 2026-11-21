from __future__ import annotations
from ._shared import *  # noqa: F401,F403
from routers.persistent_stacks import _require_platform_admin, _require_stack_author  # noqa: F401  (defined in __init__; keeps __module__ stable)

# Gate 2b: query-heavy handlers below delegate to crud/services (module-level
# import is safe here — neither module imports back into this package at
# import time; see services/persistent_stacks_task_queue_scheduler_service.py
# docstring).
from crud import persistent_stacks_task_queue_scheduler as _pts_crud  # noqa: F401
from services import persistent_stacks_task_queue_scheduler_service as _pts_svc

from fastapi import APIRouter  # noqa: F811  (local, prefix-free; the aggregator carries prefix/tags/deps)
router = APIRouter()

@router.post("/managed-ai/apply", response_model=ManagedAIApplyResponse, dependencies=[Depends(_require_platform_admin)])
def apply_managed_ai(preset: Optional[str] = None,
                     db: Session = Depends(get_db),
                     user: models.User = Depends(get_current_user)):
    """managed-ai-self-deploy — make a tier selection ACT, not just save.

    The System-managed bar previously saved a preset and then told the operator
    to run `docker compose up -d --force-recreate vllm` by hand, because the
    preset was read from MANAGED_OLLAMA_PRESET at process start and the API has
    no docker socket. That reasoning was true for the COMPOSE path and never
    true for the STACK path: a stack is deployed by dispatching a task to a
    worker, which is something the API does all day.

    So this rewrites the managed-AI stack definitions at the requested preset
    and redeploys them. `definitions(preset=...)` already accepted an explicit
    preset; nothing needed inventing.

    Idempotent: re-running with the same preset re-provisions the same specs and
    redeploys, which is also the recovery path when a deploy was lost.
    """
    return _pts_svc.apply_managed_ai(db, user, preset)

@router.get("/accelerator-capacity", dependencies=[Depends(_require_platform_admin)])
def accelerator_capacity(db: Session = Depends(get_db),
                         user: models.User = Depends(get_current_user)):
    """Per-worker accelerator capacity split into total / stack-reserved / free.

    Without this, reserved capacity is invisible: a worker hosting a vLLM stack
    still advertises "cuda", and the only symptom of its card being spoken for
    is that accelerated node tasks quietly stop being scheduled there.
    """
    return _pts_svc.accelerator_capacity(db, user)

@router.put("/reconciler-config", dependencies=[Depends(_require_platform_admin)])
def set_reconciler_config(body: _ReconcilerConfig,
                          db: Session = Depends(get_db),
                          user: models.User = Depends(get_current_user)):
    """Set automatic-recovery master enable and/or live mode (platform-admin).

    Both fields optional so each checkbox on the Health & recovery card can be
    toggled without round-tripping the other. Turning live mode ON is the
    deliberate act that lets recovery stop reporting and start acting."""
    return _pts_svc.set_reconciler_config(db, body, user)

__all__ = [
    'apply_managed_ai',
    'accelerator_capacity',
    'set_reconciler_config',
]
