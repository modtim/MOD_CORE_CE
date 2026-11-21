"""Data access for the per-tenant Named Artifact registry (named-artifact-registry).

The ONLY place `db.query(models.NamedArtifact)` lives for this domain — tenant
scoping + soft-delete filtering happen here once. See `routers/named_artifacts.py`
for the domain description and `services/named_artifacts_service.py` for policy
(type validation, upsert-vs-create, response shaping).
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_live_by_name(
    db: Session, *, tenant_id: int, art_type: str, name: str,
) -> Optional[models.NamedArtifact]:
    """Fetch a tenant's live (non-deleted) named artifact by (type, name) — the
    upsert key. Returns None if absent."""
    return (
        db.query(models.NamedArtifact)
        .filter(
            models.NamedArtifact.tenant_id == tenant_id,
            models.NamedArtifact.type == art_type,
            models.NamedArtifact.name == name,
            models.NamedArtifact.deleted_at.is_(None),
        )
        .first()
    )


def get_live_by_id(
    db: Session, *, artifact_id: int, tenant_id: int,
) -> Optional[models.NamedArtifact]:
    """Fetch a tenant's live (non-deleted) named artifact by primary key.

    The tenant filter is what makes this cross-tenant-safe: an id that
    belongs to another tenant resolves to None here (the caller maps that
    to a 404, not a 403, matching the router's cross-tenant convention)."""
    return (
        db.query(models.NamedArtifact)
        .filter(
            models.NamedArtifact.id == artifact_id,
            models.NamedArtifact.tenant_id == tenant_id,
            models.NamedArtifact.deleted_at.is_(None),
        )
        .first()
    )


def list_live(
    db: Session, *, tenant_id: int, art_type: Optional[str] = None,
) -> List[models.NamedArtifact]:
    """List a tenant's live named artifacts, optionally filtered by type,
    ordered by name. Small, per-tenant registry — not paginated (matches the
    existing WB dropdown contract)."""
    query = db.query(models.NamedArtifact).filter(
        models.NamedArtifact.tenant_id == tenant_id,
        models.NamedArtifact.deleted_at.is_(None),
    )
    if art_type is not None:
        query = query.filter(models.NamedArtifact.type == art_type)
    return query.order_by(models.NamedArtifact.name.asc()).all()
