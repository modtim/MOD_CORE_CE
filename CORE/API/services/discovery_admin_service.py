"""Service layer for the discovery admin router.

This module provides data access functions for the human-assisted discovery
endpoints (learned crossovers review queue and weak-verdict misses).
All functions return raw query results that the router shapes into response models.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_learned_crossovers(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    approved: Optional[bool] = None,
    limit: int,
) -> List[models.WizardLearnedCrossover]:
    """List learned crossovers, newest first, with optional filters.

    Args:
        db: Database session.
        tenant_id: Optional filter to one tenant.
        approved: Optional filter by approved_cross_tenant.
        limit: Maximum rows to return (1-500).

    Returns:
        List of WizardLearnedCrossover rows, newest first.
    """
    q = db.query(models.WizardLearnedCrossover)
    if tenant_id is not None:
        q = q.filter(models.WizardLearnedCrossover.tenant_id == tenant_id)
    if approved is not None:
        q = q.filter(
            models.WizardLearnedCrossover.approved_cross_tenant == approved
        )
    return (
        q.order_by(models.WizardLearnedCrossover.created_at.desc())
        .limit(limit)
        .all()
    )


def get_learned_crossover_by_id(
    db: Session, *, crossover_id: int
) -> Optional[models.WizardLearnedCrossover]:
    """The learned crossover with *crossover_id*, or None."""
    return (
        db.query(models.WizardLearnedCrossover)
        .filter(models.WizardLearnedCrossover.id == crossover_id)
        .first()
    )


def get_learned_crossovers_by_hit_count(
    db: Session, *, limit: int
) -> List[models.WizardLearnedCrossover]:
    """Top learned crossovers by hit_count (fallback for misses endpoint).

    Returns rows with hit_count > 0, ordered by hit_count descending.
    Used as a proxy when the in-memory weak-miss counter is empty.
    """
    return (
        db.query(models.WizardLearnedCrossover)
        .filter(models.WizardLearnedCrossover.hit_count > 0)
        .order_by(models.WizardLearnedCrossover.hit_count.desc())
        .limit(limit)
        .all()
    )
