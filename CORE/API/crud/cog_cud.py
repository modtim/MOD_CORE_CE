"""Cog CRUD operations - Cog management within tenants."""
from typing import Optional, List

from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone

import models, schemas


def create_cog(
    db: Session,
    cog: schemas.CogCreate,
    user_id: int,
    tenant_id: int
) -> models.Cog:
    """Create a new cog."""
    db_cog = models.Cog(**cog.model_dump(), owner_id=user_id, tenant_id=tenant_id)
    db.add(db_cog)
    db.commit()
    db.refresh(db_cog)
    # Gate 24: a folder owner holds a real OWNER grant (no-op for FILE cogs, which
    # keep ownership via files.user_id). Removes the need for an owner short-circuit.
    from resource_permissions import grant_cog_owner_if_folder
    grant_cog_owner_if_folder(db, db_cog)
    db.commit()
    return db_cog


def get_cogs(
    db: Session,
    owner_id: int,
    parent_id: Optional[int] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "name",
    order: str = "asc",
) -> List[models.Cog]:
    """Retrieve cogs for a specific owner with optional filtering, pagination, and sorting."""
    query = db.query(models.Cog).filter(
        models.Cog.owner_id == owner_id,
        models.Cog.deleted_at.is_(None)
    )

    query = query.filter(models.Cog.parent_id == parent_id)

    if search:
        query = query.filter(models.Cog.name.ilike(f"%{search}%"))

    # Apply sorting
    if hasattr(models.Cog, sort_by):
        column = getattr(models.Cog, sort_by)
        if order == "desc":
            query = query.order_by(column.desc())
        else:
            query = query.order_by(column.asc())

    cogs = query.offset(offset).limit(limit).all()
    return cogs


def get_cog_by_id(db: Session, cog_id: int, owner_id: int) -> Optional[models.Cog]:
    """Retrieve a single cog by its ID, ensuring it belongs to the specified owner."""
    return (
        db.query(models.Cog)
        .filter(
            models.Cog.id == cog_id,
            models.Cog.owner_id == owner_id,
            models.Cog.deleted_at.is_(None)
        )
        .first()
    )


def get_cog_by_id_and_tenant(
    db: Session,
    cog_id: int,
    tenant_id: int
) -> Optional[models.Cog]:
    """Retrieve a single cog by its ID, ensuring it belongs to the specified tenant."""
    return (
        db.query(models.Cog)
        .filter(models.Cog.id == cog_id, models.Cog.tenant_id == tenant_id, models.Cog.deleted_at.is_(None))
        .first()
    )


def update_cog(
    db: Session,
    db_cog: models.Cog,
    cog_update: schemas.CogUpdate
) -> models.Cog:
    """Update a cog's attributes in the database."""
    update_data = cog_update.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_cog, key, value)
    db.add(db_cog)
    db.commit()
    db.refresh(db_cog)
    return db_cog


def delete_cog(db: Session, db_cog: models.Cog) -> models.Cog:
    """
    Soft deletes a cog by setting the 'deleted_at' field.
    Also soft-deletes associated permissions for ISO 27001 audit trail compliance.
    """
    # Gate 30: revoke this cog's ResourcePermission grants (soft delete via
    # revoked_at/is_active, preserving the audit trail). Covers shares and owner
    # grants, so a deleted cog leaves no live access rows.
    db.query(models.ResourcePermission).filter(
        models.ResourcePermission.resource_type == models.ResourceType.COG,
        models.ResourcePermission.resource_id == db_cog.id,
        models.ResourcePermission.is_active == True,
        models.ResourcePermission.revoked_at.is_(None)
    ).update({"is_active": False, "revoked_at": datetime.now(timezone.utc)},
             synchronize_session=False)

    db_cog.deleted_at = datetime.now(timezone.utc)
    db.add(db_cog)
    db.commit()
    db.refresh(db_cog)
    return db_cog


def soft_delete_cogs_recursive(
    db: Session,
    parent_id: int,
    owner_id: int
):
    """
    Recursively soft-deletes all children of a given parent cog.
    Skips audit-protected (is_locked=True) cogs to preserve compliance/audit trail.
    """
    children = db.query(models.Cog).filter(
        models.Cog.parent_id == parent_id,
        models.Cog.owner_id == owner_id,
        models.Cog.deleted_at.is_(None)
    ).all()
    for child in children:
        # Skip audit-protected items (workflow folders, runs, steps)
        if child.is_locked:
            continue
        if child.cog_type in [models.CogType.MASTER_FOLDER, models.CogType.ASSET_FOLDER]:
            soft_delete_cogs_recursive(db, parent_id=child.id, owner_id=owner_id)
        delete_cog(db, db_cog=child)


def create_cog_version(db: Session, cog_id: int, version: schemas.VersionCreate) -> models.Version:
    """Creates a new version record for a cog in the database."""
    db_version = models.Version(
        cog_id=cog_id,
        tenant_id=version.tenant_id,
        version_number=version.version_number,
        content_id=version.content_id,
    )
    db.add(db_version)
    db.commit()
    db.refresh(db_version)
    return db_version


def get_cog_versions(db: Session, cog_id: int, limit: int = 50, offset: int = 0) -> List[models.Version]:
    """Retrieves all versions for a specific cog with pagination."""
    return (
        db.query(models.Version)
        .filter(models.Version.cog_id == cog_id)
        .order_by(models.Version.version_number.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
