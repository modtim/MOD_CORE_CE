"""Tag CRUD operations - Tag management within tenants."""
from typing import Optional, List

from sqlalchemy.orm import Session
import models, schemas
from sqlalchemy.exc import IntegrityError


def create_tag(db: Session, tag: schemas.TagCreate, tenant_id: int) -> models.Tag:
    """Create a new tag for a specific tenant."""
    db_tag = models.Tag(name=tag.name, tenant_id=tenant_id)
    db.add(db_tag)
    try:
        db.commit()
        db.refresh(db_tag)
        return db_tag
    except IntegrityError:
        db.rollback()
        # Re-raise the exception to be handled in the router layer
        raise


def get_tags_for_tenant(
    db: Session,
    tenant_id: int,
    limit: int = 100,
    offset: int = 0,
    sort_by: str = "name",
    order: str = "asc",
) -> List[models.Tag]:
    """Retrieve a paginated and sorted list of tags for a specific tenant."""
    query = db.query(models.Tag).filter(models.Tag.tenant_id == tenant_id)

    # Ensure sort_by is a valid attribute to prevent SQL injection
    if hasattr(models.Tag, sort_by):
        column = getattr(models.Tag, sort_by)
        if order.lower() == "desc":
            query = query.order_by(column.desc())
        else:
            query = query.order_by(column.asc())

    return query.offset(offset).limit(limit).all()


def get_tags_for_cog(db: Session, cog_id: int) -> List[models.Tag]:
    """Retrieve all tags associated with a specific cog."""
    cog = db.query(models.Cog).filter(
        models.Cog.id == cog_id,
        models.Cog.deleted_at.is_(None)
    ).first()
    if cog:
        return cog.tags
    return []


def add_tag_to_cog(db: Session, db_cog: models.Cog, db_tag: models.Tag) -> models.Cog:
    """Associate a tag with a cog in the database."""
    db_cog.tags.append(db_tag)
    db.commit()
    db.refresh(db_cog)
    return db_cog


def remove_tag_from_cog(db: Session, db_cog: models.Cog, db_tag: models.Tag) -> models.Cog:
    """Remove the association between a tag and a cog."""
    db_cog.tags.remove(db_tag)
    db.commit()
    db.refresh(db_cog)
    return db_cog
