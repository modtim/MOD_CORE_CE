"""Service layer for workflow-completion callbacks (Gate 2b migration of
`routers/workflow_callbacks.py`).

Owns the tenant-ownership policy (404-vs-403), the status-mapping decision
for the conditional terminal transition, and the best-effort outbound-webhook
fan-out. Data access goes through `crud/workflow_callbacks.py`.

NOTE on `decrement_concurrent_processes`: that call deliberately STAYS in the
router. `routers/workflow_callbacks.py` imports it directly from
`utils.usage_enforcement` and several existing tests patch it at
`routers.workflow_callbacks.decrement_concurrent_processes` (migrate_rules
#7: preserve external patch sites). Moving the call into this module would
silently break those patches (they patch the router module's attribute, not
this one's), so this module only returns a `should_decrement` decision and
the router performs that one side effect itself.
"""
from __future__ import annotations

from product_names import themed_logger
from typing import Any, NamedTuple, Optional

import models
from crud import workflow_callbacks as crud
from services._base import NotFoundError, ServiceError

logger = themed_logger(__name__, 'workflows')

# "success"/"failed"/"canceled" (from the orchestrator) -> terminal TaskStatus.
# Canceled is treated as failed, matching the pre-migration handler.
_STATUS_MAP = {
    "success": models.TaskStatus.COMPLETED,
    "failed": models.TaskStatus.FAILED,
    "canceled": models.TaskStatus.FAILED,
}


class ForbiddenError(ServiceError):
    """Caller's authenticated tenant does not own the run (router maps ->
    403). `services/_base.py` ships `NotFoundError` / `ConflictError` but no
    forbidden error — declared here per migrate_rules #5."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class CompletionResult(NamedTuple):
    workflow_run_id: int
    should_decrement: bool
    new_status: Optional["models.TaskStatus"]


def process_completion(
    db,
    *,
    workflow_run_id: int,
    status_str: str,
    tenant_id: int,
) -> CompletionResult:
    """Validate ownership, apply the conditional terminal-status transition,
    and (best-effort, non-fatal) fan out outbound webhooks.

    Raises `NotFoundError` (-> 404) if the run doesn't exist, `ForbiddenError`
    (-> 403) if it belongs to a different tenant than the authenticated
    caller. Returns a `CompletionResult` telling the router whether it should
    decrement the tenant's concurrent-process counter.
    """
    run = crud.get_workflow_run_by_id(db, workflow_run_id)
    if run is None:
        raise NotFoundError("Workflow run not found")
    if run.tenant_id != tenant_id:
        raise ForbiddenError("Access denied")

    logger.info(
        f"📥 Workflow completion callback: run_id={workflow_run_id}, status={status_str}"
    )

    new_status = _STATUS_MAP.get(status_str.lower())
    should_decrement = False
    if new_status:
        rowcount = crud.transition_to_terminal(db, workflow_run_id, new_status)
        if rowcount > 0:
            should_decrement = True
            logger.info(
                f"✅ Updated workflow run {workflow_run_id} status to {new_status.value}"
            )
        else:
            logger.warning(
                f"⚠️ Workflow run {workflow_run_id} was already in terminal state, skipping update"
            )

    if should_decrement:
        # Fire HMAC-signed outbound webhooks to tenant subscribers. Best-effort
        # and strictly non-fatal — a delivery failure must never fail the
        # completion callback.
        try:
            from services.outbound_webhooks import dispatch_workflow_completion

            dispatch_workflow_completion(
                db=db,
                tenant_id=tenant_id,
                workflow_run_id=workflow_run_id,
                status_str=status_str,
            )
        except Exception:
            logger.exception("outbound webhook dispatch failed (non-fatal)")

    return CompletionResult(
        workflow_run_id=workflow_run_id,
        should_decrement=should_decrement,
        new_status=new_status,
    )
