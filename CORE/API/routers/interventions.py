# API/routers/interventions.py
"""Gate 5 — a pipeline can wait for a human.

The waiting itself is a row in `manual_intervention_states`, written by
`workflow_orchestrator.park_for_human_approval` instead of dispatching a task.
This module is the other half: how a person sees what is waiting on them and
answers it.

RESUMING IS NOT A NEW MECHANISM. `_check_and_dispatch_downstream` already
advances the DAG event-driven whenever something finishes — the path Gate 3
proved on a real sixty-step run. An approval therefore completes the parked
NodeRun and calls that same function. There is no suspended-run state machine
and no sweeper polling for decisions, because the DAG already knows how to move
when a node finishes and a human finishing is not special.

A rejection fails the run. That is the honest reading of a QA gate: the point of
asking was that the answer might be no, and a "no" that let delivery proceed
would make the gate decorative.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from database import SessionLocal
from dependencies import require_active_and_onboarded_user
from services import interventions_service
from services._base import ServiceError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/interventions", tags=["interventions"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ServiceError -> HTTPException mapping (api_conventions.install_error_handlers
# is scaffold-only / not wired into main.py yet -- see api_conventions.py
# docstring -- so handlers here map inline, same as the domain/reference
# example in services/_reference_domain_example.py).
# ---------------------------------------------------------------------------

_SERVICE_ERROR_STATUS = {
    "forbidden": status.HTTP_403_FORBIDDEN,
    "not_found": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
    "internal_error": status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _raise_http(exc: ServiceError) -> None:
    raise HTTPException(
        status_code=_SERVICE_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.message,
    )


class InterventionOut(BaseModel):
    id: int
    node_run_id: Optional[int] = None
    workflow_run_id: Optional[int] = None
    workflow_id: Optional[int] = None
    workflow_version_id: Optional[int] = None
    status: str
    instructions: Optional[str] = None
    completion_notes: Optional[str] = None
    assigned_to_user_id: Optional[int] = None
    assigned_at: Optional[str] = None


class DecisionIn(BaseModel):
    notes: Optional[str] = None


class AssignableUser(BaseModel):
    id: int
    label: str


@router.get("/assignable-users", response_model=List[AssignableUser])
def assignable_users(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Who a `human_approval` node may name as its approver.

    Its own endpoint because the existing user list
    (`/resource-permissions/users/summary`) is platform-admin only, and the
    person who needs this is the workflow AUTHOR — routing an approval is not
    an administrative act. Scoped to the caller's tenant and to ACTIVE users:
    naming a deactivated account would park a run on someone who cannot answer.

    Declared before `/{intervention_id}/...` so the literal path is never
    matched as an id.
    """
    return interventions_service.list_assignable_users(db, current_user)


@router.get("", response_model=List[InterventionOut])
def list_my_interventions(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """What is waiting on me. Wires up `crud.get_user_assigned_tasks`, which
    was written with the model and had no caller."""
    return interventions_service.list_my_interventions(
        db, current_user, limit=limit, offset=offset,
    )


@router.post("/{intervention_id}/approve", response_model=InterventionOut)
def approve(
    intervention_id: int,
    body: DecisionIn = DecisionIn(),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Approve, and let the DAG carry on from the parked node."""
    try:
        return interventions_service.approve(db, intervention_id, current_user, body.notes)
    except ServiceError as exc:
        _raise_http(exc)


@router.post("/{intervention_id}/reject", response_model=InterventionOut)
def reject(
    intervention_id: int,
    body: DecisionIn = DecisionIn(),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Reject, and fail the run. Nothing downstream is dispatched."""
    try:
        return interventions_service.reject(db, intervention_id, current_user, body.notes)
    except ServiceError as exc:
        _raise_http(exc)
