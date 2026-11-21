"""Phase 10 — admin analytics over PortOutcome aggregations.

Admin-only endpoints that surface the data the Phase 6 telemetry +
Phase 8 arch_mapping rows have been accumulating, so platform
operators can spot:

  GET /system/port-analytics/failure-clusters
      Top-N (unsupported_api, variant) pairs ordered by frequency.
      Drives "what breaks the porter most often" prioritisation.

  GET /system/port-analytics/success-rate
      Per-porter_version success rate; tied to PortOutcome rows.
      Catches regressions across PORTER_VERSION transitions
      ("v1:hipify=6.2.1" → "v1:hipify=6.3.0" introduced 12% more
      not_recommended verdicts).

  GET /system/port-analytics/arch-mappings
      Output of `utils.arch_mapping_index.build_index` — surfaces
      the LLM-discovered HF→OV mappings + their observation counts.
      Reviewed before nightly promotion.

  GET /system/port-analytics/promotion-queue
      Subset of arch-mappings that pass the ≥5 obs / ≥2 tenants
      threshold for baking into the static allowlist. Phase 10's
      nightly job (CORE/scripts/jobs/recipe_evolution_nightly.py)
      reads this same data.

All endpoints are platform-admin-only and read-only.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services.port_analytics_service import (
    count_by_version_variant_compat,
    list_failed_outcomes_since,
)


# ------------------------------------------------------------------
# Response shapes — mirrors each handler's return dict exactly.
# Dynamic sub-structures use Dict[str, Any] / List[Dict[str, Any]].
# ------------------------------------------------------------------

class _ClusterItem(BaseModel):
    api: str
    variant: str
    count: int
    tenant_count: int
    compat_breakdown: Dict[str, int]


class PortFailureClustersResponse(BaseModel):
    since_days: int
    total_failure_rows: int
    top_clusters: List[_ClusterItem]


class PortSuccessRateItem(BaseModel):
    porter_version: str
    variant: str
    total: int
    compatible: int
    not_recommended: int
    incompatible: int
    success_rate: float


class PortSuccessRateResponse(BaseModel):
    since_days: int
    rows: List[PortSuccessRateItem]


class ArchMappingsResponse(BaseModel):
    since_days: int
    mappings: Dict[str, Any]


class PromotionQueueResponse(BaseModel):
    since_days: int
    min_count: int
    min_tenants: int
    candidates: Dict[str, Any]

logger = logging.getLogger(__name__)


router = APIRouter(
    tags=["System - Port Analytics"],
    prefix="/system/port-analytics",
)
# clean-v2-api (admin dissolve wave): moved from `/admin/port-analytics/*` to
# `/system/port-analytics/*` — no frontend caller found (grep-verified), so
# no back-compat alias needed.


def require_platform_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform admin privileges required",
        )
    return current_user


def _cutoff(since_days: int) -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=max(1, since_days))


@router.get("/failure-clusters", response_model=PortFailureClustersResponse)
def failure_clusters(
    since_days: int = Query(default=30, ge=1, le=365),
    top_n: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    _admin = Depends(require_platform_admin),
) -> Dict[str, Any]:
    """Aggregate the top failing CUDA APIs across PortOutcome rows.

    Walks `diagnostics.unsupported_apis` (a list per row) — Postgres
    JSONB doesn't natively explode arrays into a SUM-friendly shape
    via ORM, so we materialise rows into Python and aggregate. For
    the row counts we expect (~tens of thousands), this fits in a
    single query without an issue. A future optimization is a
    materialized view; not needed today.
    """
    cutoff = _cutoff(since_days)
    rows = list_failed_outcomes_since(db, cutoff=cutoff)

    cluster: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        diag = r.diagnostics or {}
        if not isinstance(diag, dict):
            continue
        apis = diag.get("unsupported_apis") or []
        if not isinstance(apis, list):
            continue
        for api in apis:
            if not isinstance(api, str) or not api:
                continue
            key = f"{api}::{r.variant}"
            slot = cluster.setdefault(key, {
                "api": api,
                "variant": r.variant,
                "count": 0,
                "tenants": set(),
                "compat_breakdown": {},
            })
            slot["count"] += 1
            if r.tenant_id is not None:
                slot["tenants"].add(int(r.tenant_id))
            slot["compat_breakdown"][r.compatibility] = (
                slot["compat_breakdown"].get(r.compatibility, 0) + 1
            )

    items = []
    for slot in cluster.values():
        items.append({
            "api": slot["api"],
            "variant": slot["variant"],
            "count": slot["count"],
            "tenant_count": len(slot["tenants"]),
            "compat_breakdown": slot["compat_breakdown"],
        })
    items.sort(key=lambda r: (-r["count"], r["api"]))
    return {
        "since_days": since_days,
        "total_failure_rows": len(rows),
        "top_clusters": items[:top_n],
    }


@router.get("/success-rate", response_model=PortSuccessRateResponse)
def success_rate(
    since_days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    _admin = Depends(require_platform_admin),
) -> Dict[str, Any]:
    """Per-(porter_version, variant) success rate. compat="compatible"
    counts as success; "not_recommended" + "incompatible" both fail.

    Lets ops see when a porter bump regresses — e.g. introducing
    `v1:hipify=6.3.0` and noticing the rocm not_recommended rate
    jumped 15% against the previous `v1:hipify=6.2.1`."""
    cutoff = _cutoff(since_days)
    rows = count_by_version_variant_compat(db, cutoff=cutoff)

    # { (porter_version, variant): { compat: count, total: int } }
    bucket: Dict[tuple, Dict[str, Any]] = {}
    for ver, variant, compat, count in rows:
        key = (ver or "v1", variant or "?")
        slot = bucket.setdefault(key, {
            "compatible": 0,
            "not_recommended": 0,
            "incompatible": 0,
            "total": 0,
        })
        if compat in slot:
            slot[compat] = int(count)
        slot["total"] += int(count)

    out = []
    for (ver, variant), slot in bucket.items():
        total = max(1, slot["total"])
        out.append({
            "porter_version": ver,
            "variant": variant,
            "total": slot["total"],
            "compatible": slot["compatible"],
            "not_recommended": slot["not_recommended"],
            "incompatible": slot["incompatible"],
            "success_rate": slot["compatible"] / total,
        })
    out.sort(key=lambda r: (r["variant"], r["porter_version"]))
    return {
        "since_days": since_days,
        "rows": out,
    }


@router.get("/arch-mappings", response_model=ArchMappingsResponse)
def arch_mappings(
    since_days: int = Query(default=90, ge=1, le=365),
    min_confidence: float = Query(default=0.6, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
    _admin = Depends(require_platform_admin),
) -> Dict[str, Any]:
    """Cross-tenant index of LLM-discovered HF→OV architecture
    mappings. Wraps `utils.arch_mapping_index.build_index`."""
    from utils.arch_mapping_index import build_index
    idx = build_index(db, since_days=since_days, min_confidence=min_confidence)
    return {"since_days": since_days, "mappings": idx}


@router.get("/promotion-queue", response_model=PromotionQueueResponse)
def promotion_queue(
    since_days: int = Query(default=90, ge=1, le=365),
    min_count: int = Query(default=5, ge=1),
    min_tenants: int = Query(default=2, ge=1),
    db: Session = Depends(get_db),
    _admin = Depends(require_platform_admin),
) -> Dict[str, Any]:
    """Subset of arch mappings that pass the safe-to-promote
    threshold. The Phase 10 nightly job reads the same data; this
    endpoint lets a human review the queue before the next promotion
    cycle commits the changes into the static allowlist."""
    from utils.arch_mapping_index import build_index, promotion_candidates
    idx = build_index(db, since_days=since_days)
    promoted = promotion_candidates(idx, min_count=min_count, min_tenants=min_tenants)
    return {
        "since_days": since_days,
        "min_count": min_count,
        "min_tenants": min_tenants,
        "candidates": promoted,
    }
