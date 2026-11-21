# In Main/routers/teams.py

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

import crud, models, schemas
from dependencies import get_db, require_active_and_onboarded_user, require_capability

router = APIRouter(
    tags=["Teams"],
    dependencies=[Depends(require_active_and_onboarded_user)],
    responses={404: {"description": "Not found"}},
)


@router.post("/", response_model=schemas.Team, status_code=status.HTTP_201_CREATED)
def create_team(
    team: schemas.TeamCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_capability(models.ResourceType.TEAM, "create"))
):
    """
    Create a new team within the current user's tenant.
    Team names must be unique per tenant.
    """
    try:
        return crud.create_team(db=db, team=team, tenant_id=current_user.tenant_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A team with this name already exists in your tenant.",
        )


@router.get("/", response_model=List[schemas.Team])
def read_teams(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_capability(models.ResourceType.TEAM, "view"))
):
    """
    Retrieve all teams within the current user's tenant.
    """
    return crud.get_teams(db, tenant_id=current_user.tenant_id, skip=skip, limit=limit)


@router.get("/{team_id}", response_model=schemas.Team)
def read_team(
    team_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_capability(models.ResourceType.TEAM, "view_members"))
):
    """
    Retrieve a specific team from the current user's tenant.
    """
    db_team = crud.get_team(db, team_id=team_id, tenant_id=current_user.tenant_id)
    if db_team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    return db_team


@router.post("/{team_id}/members", response_model=schemas.Team)
def add_team_member(
    team_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_capability(models.ResourceType.TEAM, "add_members"))
):
    """
    Add a user to a team. The user must belong to the same tenant.
    """
    db_team = crud.get_team(db, team_id=team_id, tenant_id=current_user.tenant_id)
    if db_team is None:
        raise HTTPException(status_code=404, detail="Team not found")

    # Crucially, verify the user to be added is also in the same tenant
    db_user = crud.get_user_by_id_for_tenant(db, user_id=user_id, tenant_id=current_user.tenant_id)
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found in this tenant")

    return crud.add_team_member(db=db, db_team=db_team, db_user=db_user)