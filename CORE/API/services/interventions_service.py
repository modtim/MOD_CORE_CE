# API/services/interventions_service.py
"""Business logic for the manual-intervention (Gate 5 human-approval)
domain (routers/interventions.py).

Owns the transaction boundary and the downstream-dispatch / notification
calls that resume or fail a run from a decision. `crud.interventions` is
the only place `db.query(models.X)` runs for this domain now -- see that
module's docstring for the two exceptions ported as-is
(`crud.get_user_assigned_tasks` in crud/user_cud.py, and
`ManualInterventionState` having no tenant_id/deleted_at column).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import crud
import models
from crud import interventions as interventions_crud
from services._base import ConflictError, NotFoundError, ServiceError

logger = logging.getLogger(__name__)


class ForbiddenError(ServiceError):
    """Caller isn't the assignee of the intervention they're trying to
    decide (router maps -> 403). `services/_base.py` declares no
    forbidden error (see the 404-vs-403 trap note in the Gate-2
    migration rules), so it's declared here instead."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


def list_assignable_users(db: Session, current_user: models.User) -> List[Dict[str, Any]]:
    """Who a `human_approval` node may name as its approver.

    Its own endpoint because the existing user list
    (`/resource-permissions/users/summary`) is platform-admin only, and the
    person who needs this is the workflow AUTHOR — routing an approval is not
    an administrative act. Scoped to the caller's tenant and to ACTIVE users:
    naming a deactivated account would park a run on someone who cannot answer.
    """
    rows = interventions_crud.list_active_tenant_users(db, current_user.tenant_id)
    out = []
    for u in rows:
        name = " ".join(p for p in ((u.first_name or ""), (u.last_name or "")) if p).strip()
        label = name or (u.username or "") or (u.email or "") or f"User {u.id}"
        if name and u.email:
            label = f"{name} ({u.email})"
        out.append({"id": u.id, "label": label})
    return out


def _run_refs_for(db: Session, iv: models.ManualInterventionState):
    """(run_id, workflow_id, version_id) so the UI can OPEN the paused run.

    An approval is a decision about a specific workflow, and answering it from
    a list without being able to look at what you are approving is a worse
    version of the same problem the gate exists to solve.
    """
    if iv.node_run_id is None:
        return None, None, None
    node_run = db.get(models.NodeRun, iv.node_run_id)
    if node_run is None:
        return None, None, None
    run = db.get(models.WorkflowRun, node_run.workflow_run_id)
    if run is None:
        return node_run.workflow_run_id, None, None
    return run.id, run.workflow_id, run.workflow_version_id


def serialise(db: Session, iv: models.ManualInterventionState) -> Dict[str, Any]:
    _run_id, _wf_id, _ver_id = _run_refs_for(db, iv)
    return {
        "id": iv.id,
        "node_run_id": iv.node_run_id,
        "workflow_run_id": _run_id,
        "workflow_id": _wf_id,
        "workflow_version_id": _ver_id,
        "status": iv.status.value if iv.status else "",
        "instructions": iv.instructions,
        "completion_notes": iv.completion_notes,
        "assigned_to_user_id": iv.assigned_to_user_id,
        "assigned_at": iv.assigned_at.isoformat() if iv.assigned_at else None,
    }


def list_my_interventions(
    db: Session, current_user: models.User, *, limit: int, offset: int,
) -> List[Dict[str, Any]]:
    """What is waiting on the caller. Wires up `crud.get_user_assigned_tasks`,
    which was written with the model and had no caller."""
    rows = crud.get_user_assigned_tasks(
        db, user_id=current_user.id, limit=limit, offset=offset,
    )
    return [serialise(db, iv) for iv in rows]


def _load_for_decision(
    db: Session, intervention_id: int, current_user: models.User,
) -> models.ManualInterventionState:
    """Fetch an intervention this user is allowed to decide, or raise.

    Deciding twice is a 409 rather than a silent no-op: the second caller
    believes they resumed the run, and the DAG has already moved on from the
    first decision. Telling them nothing happened is worse than an error.
    """
    iv = db.get(models.ManualInterventionState, intervention_id)
    if iv is None:
        raise NotFoundError("Intervention not found")
    if iv.assigned_to_user_id != current_user.id:
        raise ForbiddenError("This approval is assigned to another user")
    if iv.status != models.InterventionStatus.PENDING_ACTION:
        raise ConflictError(f"Already decided: {iv.status.value}")
    return iv


def approve(
    db: Session,
    intervention_id: int,
    current_user: models.User,
    notes: Optional[str],
) -> Dict[str, Any]:
    """Approve, and let the DAG carry on from the parked node."""
    from routers.worker_tasks import _check_and_dispatch_downstream
    from services.workflow_orchestrator import _emit_run_event

    iv = _load_for_decision(db, intervention_id, current_user)
    iv.status = models.InterventionStatus.ACTION_TAKEN
    iv.completion_notes = notes
    iv.completed_at = datetime.now(timezone.utc)

    node_run = db.get(models.NodeRun, iv.node_run_id) if iv.node_run_id else None
    workflow_run_id = node_run.workflow_run_id if node_run else None
    if node_run is not None:
        node_run.status = models.TaskStatus.SUCCESS

    if workflow_run_id is not None:
        _emit_run_event(
            db, workflow_run_id, "orchestrator", "info",
            f"Approved by user {current_user.id}"
            + (f" — {notes}" if notes else ""),
            {"intervention_id": iv.id},
        )
    # NOT notified on approval. The platform already tells the requester when
    # their run finishes — measured: approving produced three notifications
    # for the maker, the last being 'Workflow "…" completed successfully'.
    # A second message saying the gate opened is noise on a surface whose
    # value depends on being worth reading. Rejection is different and is
    # handled there: nothing else announces it.
    db.commit()

    # The row is PERSISTED, and it is created here rather than at park time.
    #
    # `_check_and_dispatch_downstream` decides a downstream node is ready by
    # querying for a COMPLETED ApiTaskQueue row per parent. A parked approval
    # has no such row, so without this everything downstream of a gate is
    # permanently unreachable and the run is failed by the orphan sweep — the
    # end-to-end test failed exactly this way before the row was persisted.
    # Weakening that readiness check was the alternative and it sits on the hot
    # path of every workflow; this does not.
    #
    # Created on APPROVAL, never at park time, and born COMPLETED: a QUEUED row
    # would be claimed by the first worker to poll, which is precisely the
    # container-blocking-on-a-human that parking exists to avoid. No worker can
    # see a task that is already finished when it is written.
    if node_run is not None and workflow_run_id is not None:
        workflow_node = db.get(models.WorkflowNode, node_run.workflow_node_id)
        run = db.get(models.WorkflowRun, workflow_run_id)
        gate_task = models.ApiTaskQueue(
            workflow_run_id=workflow_run_id,
            node_run_id=node_run.id,
            node_id=workflow_node.graph_node_id if workflow_node else None,
            tenant_id=run.tenant_id if run else None,
            queue_name="CPU",
            status=models.ApiTaskStatus.COMPLETED,
            task_payload={"node_kind": "human_approval",
                          "approved_by_user_id": current_user.id},
            result_payload={"approved": True, "notes": notes},
        )
        db.add(gate_task)
        db.commit()
        db.refresh(gate_task)
        try:
            _check_and_dispatch_downstream(db, gate_task)
        except Exception:  # noqa: BLE001
            logger.exception(
                "approval %s: downstream dispatch failed for run %s",
                iv.id, workflow_run_id,
            )
            raise ServiceError(
                "Approved, but resuming the workflow failed — see run logs",
                code="internal_error",
            )

    db.refresh(iv)
    return serialise(db, iv)


def reject(
    db: Session,
    intervention_id: int,
    current_user: models.User,
    notes: Optional[str],
) -> Dict[str, Any]:
    """Reject, and fail the run. Nothing downstream is dispatched."""
    from services.workflow_orchestrator import _emit_run_event

    iv = _load_for_decision(db, intervention_id, current_user)
    iv.status = models.InterventionStatus.REJECTED
    iv.completion_notes = notes
    iv.completed_at = datetime.now(timezone.utc)

    node_run = db.get(models.NodeRun, iv.node_run_id) if iv.node_run_id else None
    workflow_run_id = node_run.workflow_run_id if node_run else None
    run: Optional[models.WorkflowRun] = None
    if node_run is not None:
        node_run.status = models.TaskStatus.FAILED

    # EVERYTHING STILL WAITING IS BLOCKED, and it has to be said out loud.
    #
    # Rejecting stops the graph, but the nodes below the gate were left at
    # QUEUED for ever — nothing dispatches them and nothing finalises them. The
    # builder treats {success, failure, blocked} as terminal, so a run holding a
    # QUEUED node never reads as finished: the poll never stops, the toolbar
    # keeps offering Cancel instead of Execute, and the workflow cannot be
    # re-run after a rejection. Reported as "it's in running mode" — the run had
    # failed hours earlier.
    #
    # BLOCKED rather than FAILED: these nodes did not fail, they were never
    # allowed to start, and the run-detail cards read very differently for the
    # person deciding what to fix.
    if workflow_run_id is not None:
        # Excluded explicitly: the gate is FAILED above, but that assignment
        # has not necessarily flushed when this query runs, so without this it
        # matches its own row and overwrites FAILED with BLOCKED. The gate was
        # rejected; the nodes below it were never allowed to start. The
        # run-detail cards should not say the same thing about both.
        exclude_id = node_run.id if node_run is not None else None
        stranded = interventions_crud.list_stranded_node_runs(
            db, workflow_run_id, exclude_node_run_id=exclude_id,
        )
        for _nr in stranded:
            _nr.status = models.TaskStatus.BLOCKED
        if stranded:
            logger.info("rejection on run %s blocked %d downstream node(s)",
                        workflow_run_id, len(stranded))

    if workflow_run_id is not None:
        run = db.get(models.WorkflowRun, workflow_run_id)
        if run is not None:
            run.status = models.TaskStatus.FAILED
            if hasattr(run, "error_message"):
                run.error_message = (
                    f"Rejected at approval gate by user {current_user.id}"
                    + (f": {notes}" if notes else "")
                )
        _emit_run_event(
            db, workflow_run_id, "orchestrator", "error",
            f"Rejected by user {current_user.id}"
            + (f" — {notes}" if notes else ""),
            {"intervention_id": iv.id},
        )
    db.commit()

    # Rejection is a workflow FAILURE, so it goes down the same road every
    # other failure does. `reject()` sets status directly instead of finishing
    # through the dispatcher, so this notification never fired — and it is not
    # only the bell: `_notify_workflow_complete` also sends the tenant's
    # configured WEBHOOK and EMAIL. A rejected run was silently skipping all
    # three, which is worse than the missing bell it was reported as.
    #
    # It notifies `triggered_by_user_id` unconditionally, including when that
    # is the person who just clicked reject. That is the platform's existing
    # answer for every other failure and it is the right one: "I clicked
    # reject" and "my pipeline has failed" are different facts, and an earlier
    # attempt to suppress the self-case here left exactly the gap that was
    # reported.
    #
    # `run` is initialised to None above and only reassigned inside the
    # `if workflow_run_id is not None:` block. When `iv.node_run_id` is None
    # (so `workflow_run_id` stays None), `run` stays None here and this is a
    # no-op, as intended -- previously an UnboundLocalError in this path.
    if run is not None:
        try:
            from routers.worker_tasks import _notify_workflow_complete
            _notify_workflow_complete(db, run, run.tenant_id)
        except Exception:  # noqa: BLE001
            logger.exception("rejection notification failed for run %s", workflow_run_id)
    db.refresh(iv)
    return serialise(db, iv)
