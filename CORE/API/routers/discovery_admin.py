# API/routers/discovery_admin.py
"""
human-assisted-discovery — operator surface.

Platform-admin gated endpoints for reviewing the crossover entries the wizard
LEARNS from human answers to describe-search clarifying questions
(models.WizardLearnedCrossover, written by
node_builder/learned_crossover.record_learned_crossover).

  - GET  /system/discovery/learned-crossovers
        List learned crossovers, newest first. Optional ?tenant_id= and
        ?approved= filters. This is the review queue: an operator inspects a
        tenant's learned entries.

  - POST /system/discovery/learned-crossovers/{id}/approve  (GONE)
        Cross-tenant promotion is disabled by tenant-isolation policy (HTTP 410).

  - GET  /system/discovery/misses
        The weak-verdict pool — descriptions that triggered a clarifying
        question. Backed by a lightweight in-memory counter that the
        describe-search path may increment (record_weak_miss). When that
        counter is empty (fresh process, or the increment hook isn't wired)
        it falls back to the highest hit_count learned crossovers as a proxy
        for "descriptions people keep needing help with".

Read-only. Cross-tenant promotion disabled by GB.LEAK.1.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services.discovery_admin_service import (
    get_learned_crossover_by_id,
    get_learned_crossovers,
    get_learned_crossovers_by_hit_count,
)

logger = logging.getLogger(__name__)

# clean-v2-api (admin dissolve wave): moved from `/admin/discovery/*` to
# `/system/discovery/*` — no frontend caller found (grep-verified), so no
# back-compat alias needed.
router = APIRouter(prefix="/system/discovery", tags=["System - Discovery"])


def require_platform_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform admin only",
        )
    return current_user


# ---------------------------------------------------------------------
# Weak-verdict miss counter (in-memory, process-local, best-effort).
#
# The describe-search path (routers/node_definitions.py, Agent A) may call
# record_weak_miss(description) whenever it emits a clarifying question. This
# is deliberately lightweight — a bounded module-level Counter, no DB writes,
# lost on restart. The /misses endpoint reads it and falls back to top
# hit_count learned crossovers when it's empty, so the surface is useful even
# before the increment hook exists.
# ---------------------------------------------------------------------
_MAX_TRACKED_MISSES = 500
_weak_misses: "Counter[str]" = Counter()


def record_weak_miss(description: str) -> None:
    """Record one weak-verdict description. Best-effort; never raises.

    Importable by the describe-search router so the operator /misses surface
    reflects live clarifying-question traffic. Bounded to avoid unbounded
    growth in a long-lived process.
    """
    try:
        desc = (description or "").strip()
        if not desc:
            return
        _weak_misses[desc] += 1
        if len(_weak_misses) > _MAX_TRACKED_MISSES:
            # Drop the least-seen entries back down to the cap.
            for key, _ in _weak_misses.most_common()[_MAX_TRACKED_MISSES:]:
                del _weak_misses[key]
    except Exception:  # pragma: no cover - defensive
        logger.debug("record_weak_miss failed", exc_info=True)


# ---------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------
class LearnedCrossoverOut(BaseModel):
    id: int
    tenant_id: int
    description: str
    jargon: List[str] = []
    github: List[str] = []
    pypi: List[str] = []
    approved_cross_tenant: bool = False
    created_by: Optional[int] = None
    hit_count: int = 0
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: "models.WizardLearnedCrossover") -> "LearnedCrossoverOut":
        return cls(
            id=row.id,
            tenant_id=row.tenant_id,
            description=row.description,
            jargon=list(row.jargon or []),
            github=list(row.github or []),
            pypi=list(row.pypi or []),
            approved_cross_tenant=bool(row.approved_cross_tenant),
            created_by=row.created_by,
            hit_count=row.hit_count or 0,
            created_at=row.created_at,
        )


class MissOut(BaseModel):
    description: str
    count: int
    source: str  # "weak_pool" | "hit_count_proxy"


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------
@router.get(
    "/learned-crossovers",
    response_model=List[LearnedCrossoverOut],
    summary="List learned crossovers (review queue)",
)
def list_learned_crossovers(
    tenant_id: Optional[int] = Query(default=None, description="Filter to one tenant"),
    approved: Optional[bool] = Query(
        default=None, description="Filter by approved_cross_tenant"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(require_platform_admin),
):
    """List learned crossovers newest-first, with optional tenant/approved
    filters. The operator review queue for promoting entries cross-tenant."""
    rows = get_learned_crossovers(
        db,
        tenant_id=tenant_id,
        approved=approved,
        limit=limit,
    )
    return [LearnedCrossoverOut.from_row(r) for r in rows]


@router.post(
    "/learned-crossovers/{crossover_id}/approve",
    response_model=LearnedCrossoverOut,
    summary="Promote a learned crossover cross-tenant",
    status_code=410,
)
def approve_learned_crossover(
    crossover_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_platform_admin),
):
    """Cross-tenant promotion is disabled by tenant-isolation policy (GB.LEAK.1).
    The route remains to preserve API contract stability, but always returns 410.
    Logs the attempt for audit purposes."""
    row = get_learned_crossover_by_id(db, crossover_id=crossover_id)
    if not row:
        logger.info(
            "admin %s attempted to promote non-existent learned_crossover=%s",
            getattr(current_user, "id", "?"), crossover_id,
        )
        raise HTTPException(
            status_code=404,
            detail=f"WizardLearnedCrossover {crossover_id} not found",
        )
    logger.info(
        "admin %s attempted to promote learned_crossover=%s cross-tenant (tenant=%s) - GONE",
        getattr(current_user, "id", "?"), crossover_id, row.tenant_id,
    )
    raise HTTPException(
        status_code=410,
        detail="Cross-tenant promotion is disabled by the tenant-isolation policy",
    )


@router.get(
    "/misses",
    response_model=List[MissOut],
    summary="Top recent weak-verdict descriptions",
)
def list_misses(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _admin: models.User = Depends(require_platform_admin),
):
    """Top descriptions that triggered a clarifying question.

    Primary source is the in-memory weak-miss counter (fed by the
    describe-search path). When that's empty — fresh process, or the
    increment hook isn't wired — falls back to the highest hit_count learned
    crossovers as a proxy for descriptions people repeatedly need help with.
    The ``source`` field says which one you're looking at.
    """
    top = _weak_misses.most_common(limit)
    if top:
        return [
            MissOut(description=desc, count=count, source="weak_pool")
            for desc, count in top
        ]

    # Fallback proxy: rows that keep getting matched are the descriptions
    # people keep asking about.
    rows = get_learned_crossovers_by_hit_count(db, limit=limit)
    return [
        MissOut(
            description=r.description,
            count=r.hit_count or 0,
            source="hit_count_proxy",
        )
        for r in rows
    ]
