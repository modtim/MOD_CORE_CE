"""Permission Templates CRUD operations.

The ONLY place `db.query(models.PermissionTemplate)` (and the couple of
`models.User` lookups the apply-template flow needs) lives for this domain.
Every read here is tenant-scoped by construction — callers never get back a
row belonging to another tenant, so a "not mine" and a "doesn't exist" both
surface as `None` (the service maps that to a 404, matching pre-migration
behavior).
"""
from typing import List, Optional

import models
from sqlalchemy.orm import Session


def list_templates(
    db: Session,
    *,
    tenant_id: int,
    include_inactive: bool = False,
) -> List[models.PermissionTemplate]:
    """All templates for a tenant, active-only unless `include_inactive`."""
    query = db.query(models.PermissionTemplate).filter(
        models.PermissionTemplate.tenant_id == tenant_id
    )
    if not include_inactive:
        query = query.filter(models.PermissionTemplate.is_active == True)  # noqa: E712
    return query.order_by(models.PermissionTemplate.name).all()


def get_template(
    db: Session, template_id: int, *, tenant_id: int
) -> Optional[models.PermissionTemplate]:
    """Fetch one template scoped to its tenant. `None` if absent or another
    tenant's row — the tenant filter lives here so the fetch can never leak
    cross-tenant existence."""
    return (
        db.query(models.PermissionTemplate)
        .filter(
            models.PermissionTemplate.id == template_id,
            models.PermissionTemplate.tenant_id == tenant_id,
        )
        .first()
    )


def get_by_name(
    db: Session,
    *,
    tenant_id: int,
    name: str,
    exclude_id: Optional[int] = None,
) -> Optional[models.PermissionTemplate]:
    """Fetch a tenant's template by exact name, optionally excluding one id
    (used by update's rename-uniqueness check)."""
    query = db.query(models.PermissionTemplate).filter(
        models.PermissionTemplate.tenant_id == tenant_id,
        models.PermissionTemplate.name == name,
    )
    if exclude_id is not None:
        query = query.filter(models.PermissionTemplate.id != exclude_id)
    return query.first()


def create_template(db: Session, values: dict) -> models.PermissionTemplate:
    """Insert a new template row. Caller passes already-validated values."""
    template = models.PermissionTemplate(**values)
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def update_template(
    db: Session, template: models.PermissionTemplate, values: dict
) -> models.PermissionTemplate:
    """Apply a partial update (only provided keys) to an existing template."""
    for field, value in values.items():
        setattr(template, field, value)
    db.commit()
    db.refresh(template)
    return template


def delete_template(db: Session, template: models.PermissionTemplate) -> None:
    """Soft-delete a permission-template row.

    iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    iso-no-hard-delete). `PermissionTemplate` now carries a `deleted_at`
    column (SoftDeleteMixin) and is in database.py's soft-delete listener, so
    the tombstoned template leaves every live read. Also flips `is_active` so
    any code that keys off that flag treats it as gone.
    """
    from datetime import datetime, timezone
    template.deleted_at = datetime.now(timezone.utc)
    template.is_active = False
    db.commit()


def record_usage(
    db: Session, template: models.PermissionTemplate, *, successful_count: int
) -> models.PermissionTemplate:
    """Bump usage stats after an apply-template run."""
    from datetime import datetime, timezone

    template.times_used += successful_count
    template.last_used_at = datetime.now(timezone.utc)
    db.commit()
    return template


def get_user_in_tenant(
    db: Session, user_id: int, *, tenant_id: int
) -> Optional[models.User]:
    """Fetch a user, scoped to a tenant (cross-tenant grant guard)."""
    return (
        db.query(models.User)
        .filter(models.User.id == user_id, models.User.tenant_id == tenant_id)
        .first()
    )


def get_user_by_email_in_tenant(
    db: Session, email: str, *, tenant_id: int
) -> Optional[models.User]:
    """Fetch a user by email, scoped to a tenant."""
    return (
        db.query(models.User)
        .filter(models.User.email == email, models.User.tenant_id == tenant_id)
        .first()
    )
