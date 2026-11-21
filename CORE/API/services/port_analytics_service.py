"""Service layer for the port analytics routers.

Data access over PortOutcome rows, shared by routers/admin_port_analytics.py
and routers/port_coverage.py. Callers compute *cutoff* themselves (each router
has its own window semantics and some echo it back in the response); these
functions only run the queries and return the raw rows for the router to shape.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

import models


def list_failed_outcomes_since(
    db: Session, *, cutoff: datetime
) -> List[models.PortOutcome]:
    """PortOutcome rows created at/after *cutoff* whose compatibility is not "compatible"."""
    return db.query(models.PortOutcome).filter(
        models.PortOutcome.created_at >= cutoff,
        models.PortOutcome.compatibility != "compatible",
    ).all()


def count_by_version_variant_compat(
    db: Session, *, cutoff: datetime
) -> List[Any]:
    """(porter_version, variant, compatibility, count) rows for outcomes at/after *cutoff*."""
    return db.query(
        models.PortOutcome.porter_version,
        models.PortOutcome.variant,
        models.PortOutcome.compatibility,
        func.count(models.PortOutcome.id),
    ).filter(
        models.PortOutcome.created_at >= cutoff,
    ).group_by(
        models.PortOutcome.porter_version,
        models.PortOutcome.variant,
        models.PortOutcome.compatibility,
    ).all()


def count_by_variant_compat(
    db: Session, *, cutoff: datetime
) -> List[Any]:
    """(variant, compatibility, count) rows for outcomes at/after *cutoff*."""
    return (
        db.query(
            models.PortOutcome.variant,
            models.PortOutcome.compatibility,
            func.count(models.PortOutcome.id),
        )
        .filter(models.PortOutcome.created_at >= cutoff)
        .group_by(models.PortOutcome.variant, models.PortOutcome.compatibility)
        .all()
    )


def top_failure_reasons(
    db: Session,
    *,
    cutoff: datetime,
    limit: int,
    variant: Optional[str] = None,
) -> List[Any]:
    """(reason, n_repos, first_seen, last_seen) for non-compatible outcomes at/after *cutoff*.

    Grouped by reason, ordered by distinct source_hash count desc, capped at *limit*.
    When *variant* is truthy it is stripped + lowercased and used as a filter.
    """
    q = (
        db.query(
            models.PortOutcome.reason,
            func.count(func.distinct(models.PortOutcome.source_hash)).label("n_repos"),
            func.min(models.PortOutcome.created_at).label("first_seen"),
            func.max(models.PortOutcome.created_at).label("last_seen"),
        )
        .filter(models.PortOutcome.created_at >= cutoff)
        .filter(models.PortOutcome.compatibility != "compatible")
        .filter(models.PortOutcome.reason.isnot(None))
    )
    if variant:
        q = q.filter(models.PortOutcome.variant == variant.strip().lower())
    q = (
        q.group_by(models.PortOutcome.reason)
        .order_by(func.count(func.distinct(models.PortOutcome.source_hash)).desc())
        .limit(limit)
    )
    return q.all()
