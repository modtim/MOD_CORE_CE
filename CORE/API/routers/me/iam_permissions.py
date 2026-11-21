"""IAM/permissions domain for the current user — team memberships + assigned tasks."""

from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import schemas, crud, models
from dependencies import get_db, require_active_and_onboarded_user

router = APIRouter()


@router.get("/teams", response_model=List[schemas.Team])
def get_my_teams(
    db: Session = Depends(get_db),
    # The other endpoints correctly use the stricter dependency.
    current_user: models.User = Depends(require_active_and_onboarded_user),
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "name",
    order: str = "asc"
):
    """
    Retrieves the teams that the current user is a member of.
    """
    return crud.get_user_teams(
        db=db, user_id=current_user.id, limit=limit, offset=offset, sort_by=sort_by, order=order
    )


@router.get("/tasks", response_model=List[schemas.ManualIntervention])
def get_my_assigned_tasks(
    db: Session = Depends(get_db),
    # The other endpoints correctly use the stricter dependency.
    current_user: models.User = Depends(require_active_and_onboarded_user),
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "assigned_at",
    order: str = "desc"
):
    """
    Retrieves pending manual intervention tasks assigned to the current user.
    """
    return crud.get_user_assigned_tasks(
        db=db, user_id=current_user.id, limit=limit, offset=offset, sort_by=sort_by, order=order
    )
