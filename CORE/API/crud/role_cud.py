"""Custom Role CRUD operations - Role management within tenants."""
from typing import Optional, List

from sqlalchemy.orm import Session
from datetime import datetime

import models, schemas


def create_custom_role(
    db: Session,
    role: schemas.CustomRoleCreate,
    tenant_id: int
) -> models.CustomRole:
    """Create a new custom role within a specific tenant."""
    db_role = models.CustomRole()
    db_role.name = role.name
    db_role.description = role.description
    db_role.tenant_id = tenant_id

    if role.capability_ids:
        capabilities = db.query(models.Capability).filter(
            models.Capability.id.in_(role.capability_ids)).all()
        db_role.capabilities = capabilities

    db.add(db_role)
    db.commit()
    db.refresh(db_role)
    return db_role


def get_custom_roles(
    db: Session,
    tenant_id: int,
    skip: int = 0,
    limit: int = 100
) -> List[models.CustomRole]:
    """Retrieve all custom roles for a specific tenant with pagination."""
    return (
        db.query(models.CustomRole)
        .filter(
            models.CustomRole.tenant_id == tenant_id,
            models.CustomRole.deleted_at.is_(None)
        )
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_custom_role(db: Session, role_id: int, tenant_id: int) -> Optional[models.CustomRole]:
    """Retrieve a single custom role by its ID, ensuring it belongs to the correct tenant."""
    return (
        db.query(models.CustomRole)
        .filter(
            models.CustomRole.id == role_id,
            models.CustomRole.tenant_id == tenant_id,
            models.CustomRole.deleted_at.is_(None)
        )
        .first()
    )


def update_custom_role(
    db: Session,
    db_role: models.CustomRole,
    role_update: schemas.CustomRoleUpdate
) -> models.CustomRole:
    """Update a custom role's name, description, and/or its associated capabilities."""
    if role_update.name is not None:
        db_role.name = role_update.name

    if role_update.description is not None:
        db_role.description = role_update.description

    if role_update.capability_ids is not None:
        capabilities = db.query(models.Capability).filter(
            models.Capability.id.in_(role_update.capability_ids)).all()
        db_role.capabilities = capabilities

    db.add(db_role)
    db.commit()
    db.refresh(db_role)
    return db_role


def delete_custom_role(db: Session, db_role: models.CustomRole) -> models.CustomRole:
    """Soft deletes a custom role by setting the 'deleted_at' field."""
    db_role.deleted_at = datetime.utcnow()
    db.add(db_role)
    db.commit()
    db.refresh(db_role)
    return db_role
