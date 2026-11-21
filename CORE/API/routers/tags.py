# In Main/routers/tags.py

from typing import List
from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

import schemas, crud, models
from dependencies import get_db, require_active_and_onboarded_user

router = APIRouter(
    tags=["Tags"],
    dependencies=[Depends(require_active_and_onboarded_user)]
)

@router.post("/", response_model=schemas.Tag, status_code=status.HTTP_201_CREATED)
def create_tag(
    tag: schemas.TagCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Create a new tag for the tenant. Tag names must be unique per tenant.
    """
    try:
        return crud.create_tag(db=db, tag=tag, tenant_id=current_user.tenant_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A tag with this name already exists for your tenant.",
        )


@router.get("/", response_model=List[schemas.Tag])
def list_tags(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
    limit: int = 100,
    offset: int = 0,
    sort_by: str = "name",
    order: str = "asc"
):
    """
    List all tags available for the tenant, with pagination and sorting.
    """
    return crud.get_tags_for_tenant(
        db=db,
        tenant_id=current_user.tenant_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        order=order
    )