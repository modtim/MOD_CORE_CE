"""Team CRUD operations - Team management within tenants."""
from typing import Optional, List

from sqlalchemy.orm import Session
from datetime import datetime

import models, schemas
from sqlalchemy.exc import IntegrityError


def create_team(db: Session, team: schemas.TeamCreate, tenant_id: int) -> models.Team:
    """Create a new team for a specific tenant."""
    db_team = models.Team()
    db_team.name = team.name
    db_team.tenant_id = tenant_id
    db.add(db_team)
    try:
        db.commit()
        db.refresh(db_team)
        return db_team
    except IntegrityError:
        db.rollback()
        # Re-raise to be handled by the router
        raise


def get_teams(db: Session, tenant_id: int, skip: int = 0, limit: int = 100) -> List[models.Team]:
    """Retrieve a paginated list of teams for a specific tenant."""
    return (
        db.query(models.Team)
        .filter(
            models.Team.tenant_id == tenant_id,
            models.Team.deleted_at.is_(None)
        )
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_team(db: Session, team_id: int, tenant_id: int) -> Optional[models.Team]:
    """Retrieve a single team by its ID, ensuring it belongs to the correct tenant."""
    return (
        db.query(models.Team)
        .filter(
            models.Team.id == team_id,
            models.Team.tenant_id == tenant_id,
            models.Team.deleted_at.is_(None)
        )
        .first()
    )


def add_team_member(db: Session, db_team: models.Team, db_user: models.User) -> models.Team:
    """Add a user to a team's list of members."""
    # Avoid adding a user who is already a member
    if db_user not in db_team.members:
        db_team.members.append(db_user)
        db.commit()
        db.refresh(db_team)
    return db_team
