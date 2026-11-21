"""P9g.3 — Port Coverage read-only endpoints.

Platform-admin dashboards for "how well does the porter work?" — per-
variant pass-rate, top failure reasons, drill-in by source_hash.

Reads from the `port_outcomes` table that P9g.1 populates on every
wizard-generation task completion. No writes; no re-running the
porter from these endpoints.

Data is intentionally platform-admin only (aggregates across all
tenants). A tenant-scoped view would live under /node_definitions/
with a tenant_id filter; add that when someone asks.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services.port_analytics_service import (
    count_by_variant_compat,
    top_failure_reasons,
)

router = APIRouter(
    prefix="/port_coverage",
    tags=["Port Coverage"],
)


# ── response models ──────────────────────────────────────────────────────────

class VariantCoverage(BaseModel):
    variant: str
    total_attempts: int
    compatible: int
    not_recommended: int
    incompatible: int
    pass_rate: float


class CoverageTotal(BaseModel):
    attempts: int
    compatible: int
    pass_rate: float


class PortCoverageSummaryResponse(BaseModel):
    window_days: int
    window_start: str
    variants: List[VariantCoverage]
    total: CoverageTotal


class FailingReason(BaseModel):
    reason: str
    distinct_repos: int
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None


class FailingImportsResponse(BaseModel):
    window_days: int
    variant: Optional[str] = None
    reasons: List[FailingReason]


# ── routes ───────────────────────────────────────────────────────────────────


def require_platform_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform admin privileges required",
        )
    return current_user


@router.get("/summary", response_model=PortCoverageSummaryResponse)
def port_coverage_summary(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_platform_admin),
):
    """Per-variant pass rate over the last N days.

    Response:
      {
        "window_days": 30,
        "window_start": "2026-03-25T...",
        "variants": [
          {
            "variant": "rocm",
            "total_attempts": 142,
            "compatible": 118,
            "not_recommended": 15,
            "incompatible": 9,
            "pass_rate": 0.83,
          },
          ...
        ],
        "total": {"attempts": ..., "compatible": ..., "pass_rate": ...}
      }

    Zero-data is well-formed: variants list is empty, totals are 0.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Aggregate per variant. Count each of the three compatibility
    # buckets; compute pass_rate = compatible / total.
    rows = count_by_variant_compat(db, cutoff=cutoff)

    per_variant: Dict[str, Dict[str, int]] = {}
    for variant, compat, count in rows:
        per_variant.setdefault(variant, {
            "compatible": 0, "not_recommended": 0, "incompatible": 0,
        })
        if compat in per_variant[variant]:
            per_variant[variant][compat] += int(count)

    variants_out: List[Dict[str, Any]] = []
    total_attempts = 0
    total_compatible = 0
    for variant, buckets in sorted(per_variant.items()):
        total = sum(buckets.values())
        total_attempts += total
        total_compatible += buckets["compatible"]
        pass_rate = buckets["compatible"] / total if total else 0.0
        variants_out.append({
            "variant": variant,
            "total_attempts": total,
            "compatible": buckets["compatible"],
            "not_recommended": buckets["not_recommended"],
            "incompatible": buckets["incompatible"],
            "pass_rate": pass_rate,
        })

    return {
        "window_days": days,
        "window_start": cutoff.isoformat(),
        "variants": variants_out,
        "total": {
            "attempts": total_attempts,
            "compatible": total_compatible,
            "pass_rate": (
                total_compatible / total_attempts if total_attempts else 0.0
            ),
        },
    }


@router.get("/failing_imports", response_model=FailingImportsResponse)
def port_coverage_failing_imports(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=200),
    variant: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_platform_admin),
):
    """Top N failing port reasons grouped. Each row is a reason string
    and the count of distinct source_hashes that failed with it —
    distinct-count avoids one loud repo with 50 re-runs dominating the
    list.

    Filterable by variant so operators can drill "what's breaking
    ROCm ports this week".

    Response:
      {
        "window_days": 30,
        "variant": "rocm" | null,
        "reasons": [
          {"reason": "hipify-clang failed on foo", "distinct_repos": 7,
           "first_seen": "...", "last_seen": "..."},
          ...
        ]
      }
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    reasons_out = []
    for reason, n_repos, first_seen, last_seen in top_failure_reasons(
        db, cutoff=cutoff, limit=limit, variant=variant,
    ):
        reasons_out.append({
            "reason": reason,
            "distinct_repos": int(n_repos),
            "first_seen": first_seen.isoformat() if first_seen else None,
            "last_seen": last_seen.isoformat() if last_seen else None,
        })

    return {
        "window_days": days,
        "variant": variant,
        "reasons": reasons_out,
    }
