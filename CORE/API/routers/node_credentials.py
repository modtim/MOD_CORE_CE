"""
Worker-mediated node credentials — API worker-mint endpoint (Group 3).

The worker calls this WORKER-authenticated endpoint to obtain a fresh, short-lived
``run_task`` token for one of its claimed tasks. The worker then relays that token
to the running node over its local, nonce-guarded token endpoint (Group 5). The
node uses it as ``X-API-Key`` on its callbacks into the API, which pin the tenant
(Group 4).

This endpoint is NOT node-facing and NOT tenant-facing: it reuses the SAME worker
authentication as the other ``/workers/tasks/...`` endpoints (``require_enrolled_worker``),
resolves the task's ``tenant_id`` and ``workflow_run_id`` server-side, and mints the
token via ``utils.build_task_token.mint_run_task_token``. The worker never signs
tokens itself.
"""

import time
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session

from dependencies import get_db
from models import WorkerLicense
from routers.worker_tasks import require_enrolled_worker, _get_worker_task
from utils.build_task_token import mint_run_task_token, is_configured

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Node Credentials"])


@router.post("/workers/tasks/{task_id}/run-token", response_model=Dict[str, Any])
async def get_task_run_token(
    task_id: int,
    worker: WorkerLicense = Depends(require_enrolled_worker),
    db: Session = Depends(get_db),
):
    """
    Mint a fresh ``run_task`` token for a claimed task.

    Worker-authenticated (same auth as the other ``/workers/tasks/...`` endpoints).
    Resolves the task's ``tenant_id`` and ``workflow_run_id`` server-side and mints
    a short-lived, tenant+run-scoped node-callback token. Returns 503 if signing is
    not configured on this deployment, 404 if the task does not exist (or 403 if it
    is not claimed by this worker).
    """
    task = _get_worker_task(db, task_id, worker.worker_name)

    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail="run_task token signing is not configured on this deployment",
        )

    token = mint_run_task_token(
        tenant_id=task.tenant_id,
        workflow_run_id=task.workflow_run_id,
    )

    # Report the token's real expiry so callers refresh with margin before it
    # dies. Decode exp from the token (no signature needed) so a non-default
    # RUN_TASK_TOKEN_TTL is honored; fall back to the default 900s TTL.
    exp = int(time.time()) + 900
    try:
        from jose import jwt as _jwt

        _exp = _jwt.get_unverified_claims(token).get("exp")
        if _exp is not None:
            exp = int(_exp)
    except Exception:  # noqa: BLE001 — exp is advisory; fall back to default TTL
        pass

    return {"token": token, "exp": exp}
