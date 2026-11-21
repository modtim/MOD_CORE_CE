"""Wizard stats dashboard endpoint.

Backs the Dashboard's System -> Wizard Info panel. All data comes
from the wizard_event_log table (durable, survives restarts) plus
the recent-failures query against workflow_runs + build_specs.

Platform-admin only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func as sa_func, text
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services.platform_wizard_stats_service import (
    get_build_spec_record_by_workflow_run_id,
    get_recent_failures,
    get_workflow_by_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_platform_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Platform admin required")
    return current_user


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class _CounterBreakdown(BaseModel):
    total: int = 0
    by_label: Dict[str, int] = {}

class _RepairCounters(BaseModel):
    total: int = 0
    infra_retry_count: int = 0
    by_outcome: Dict[str, int] = {}
    by_mode: Dict[str, int] = {}

class _VariantTestCounters(BaseModel):
    total: int = 0
    by_verdict: Dict[str, int] = {}
    by_accelerator: Dict[str, int] = {}

class _RecentFailure(BaseModel):
    workflow_run_id: Optional[int] = None
    build_spec_id: Optional[int] = None
    tenant_id: Optional[int] = None
    node_name: Optional[str] = None
    mode: Optional[str] = None
    reason: Optional[str] = None
    failed_at: Optional[datetime] = None
    repair_attempts_used: Optional[int] = None

class _WizardStatsResponse(BaseModel):
    extraction: _CounterBreakdown = _CounterBreakdown()
    extraction_by_mode: _CounterBreakdown = _CounterBreakdown()
    repair: _RepairCounters = _RepairCounters()
    llm_calls: _CounterBreakdown = _CounterBreakdown()
    image_preflight: _CounterBreakdown = _CounterBreakdown()
    variant_tests: _VariantTestCounters = _VariantTestCounters()
    speculative_queue: Dict[str, int] = {}
    recent_failures: List[_RecentFailure] = []
    window_hours: int = 24
    fetched_at: datetime


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

_SAFE_LABEL_KEYS = frozenset({
    "result", "mode", "outcome", "purpose", "verdict",
    "accelerator", "registry", "depth", "confidence", "seconds",
})

def _aggregate_events(db: Session, event_type: str, label_key: str, cutoff: datetime) -> Dict[str, int]:
    assert label_key in _SAFE_LABEL_KEYS, f"unexpected label_key: {label_key}"
    lbl_expr = text(f"metadata->>'{label_key}'")
    rows = db.execute(text(
        f"SELECT metadata->>'{label_key}' AS lbl, count(*) AS cnt "
        f"FROM wizard_event_log "
        f"WHERE event_type = :etype AND created_at >= :cutoff "
        f"GROUP BY lbl"
    ), {"etype": event_type, "cutoff": cutoff}).fetchall()
    return {r[0]: r[1] for r in rows if r[0] is not None}

def _count_events(db: Session, event_type: str, cutoff: datetime) -> int:
    row = db.execute(text(
        "SELECT count(*) FROM wizard_event_log "
        "WHERE event_type = :etype AND created_at >= :cutoff"
    ), {"etype": event_type, "cutoff": cutoff}).scalar()
    return row or 0

def _latest_per_label(db: Session, event_type: str, label_key: str, value_key: str) -> Dict[str, int]:
    assert label_key in _SAFE_LABEL_KEYS and value_key in _SAFE_LABEL_KEYS
    rows = db.execute(text(
        f"SELECT DISTINCT ON (metadata->>'{label_key}') "
        f"  metadata->>'{label_key}' AS lbl, "
        f"  metadata->>'{value_key}' AS val "
        f"FROM wizard_event_log "
        f"WHERE event_type = :etype "
        f"ORDER BY metadata->>'{label_key}', created_at DESC"
    ), {"etype": event_type}).fetchall()
    result = {}
    for r in rows:
        if r[0] is not None and r[1] is not None:
            try:
                result[r[0]] = int(float(r[1]))
            except (ValueError, TypeError):
                pass
    return result


# ---------------------------------------------------------------------------
# Recent-failures helpers
# ---------------------------------------------------------------------------

def _infer_mode_from_spec(spec_json: Dict[str, Any]) -> str:
    if not isinstance(spec_json, dict):
        return "unknown"
    bp = spec_json.get("build_plan") or {}
    if bp.get("mode"):
        return bp["mode"]
    if spec_json.get("wrap_base_image") and not spec_json.get("inline_source_body") \
            and not spec_json.get("source_repo_url"):
        return "image"
    if spec_json.get("source_repo_url"):
        return "git"
    if spec_json.get("inline_source_body"):
        return "script"
    if spec_json.get("hero_dockerfile"):
        return "legacy"
    return "unknown"


def _summarize_failure_reason(spec_json: Dict[str, Any], wf_status: Any) -> str:
    if not isinstance(spec_json, dict):
        return f"workflow_run {wf_status}"
    history = spec_json.get("repair_history") or []
    if history:
        last = history[-1]
        if isinstance(last, dict):
            reason = last.get("failure_reason") or ""
            explanation = last.get("llm_explanation") or ""
            combined = f"{reason} — {explanation}".strip(" —")
            if combined:
                return combined[:200]
    return f"workflow_run {wf_status}"


def _build_recent_failure(run: models.WorkflowRun, db: Session) -> _RecentFailure:
    """Build a _RecentFailure from a failed WorkflowRun, querying related data as needed."""
    wf = get_workflow_by_id(db, workflow_id=run.workflow_id)
    spec = get_build_spec_record_by_workflow_run_id(db, workflow_run_id=run.id)
    spec_json = (spec.spec_json or {}) if spec else {}
    return _RecentFailure(
        workflow_run_id=run.id,
        build_spec_id=spec.id if spec else None,
        tenant_id=run.tenant_id if hasattr(run, "tenant_id") else (
            spec.tenant_id if spec else None
        ),
        node_name=spec_json.get("node_name") or (
            wf.name if wf else None
        ),
        mode=_infer_mode_from_spec(spec_json),
        reason=_summarize_failure_reason(spec_json, run.status),
        failed_at=getattr(run, "completed_at", None) or getattr(run, "started_at", None),
        repair_attempts_used=(
            int(spec_json.get("repair_attempts_outer_used", 0))
            if spec_json else None
        ),
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/platform/wizard-stats",
    response_model=_WizardStatsResponse,
)
def get_wizard_stats(
    window_hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=10, ge=1, le=50),
    _admin: models.User = Depends(_require_platform_admin),
    db: Session = Depends(get_db),
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    extraction_by_result = _aggregate_events(db, "extraction", "result", cutoff)
    extraction_by_mode = _aggregate_events(db, "extraction", "mode", cutoff)
    extraction_total = sum(extraction_by_result.values())

    repair_by_outcome = _aggregate_events(db, "repair", "outcome", cutoff)
    repair_by_mode = _aggregate_events(db, "repair", "mode", cutoff)
    repair_total = sum(repair_by_outcome.values())
    infra_retry_count = repair_by_outcome.get("infra-retry", 0)

    llm_by_purpose = _aggregate_events(db, "llm_call", "purpose", cutoff)
    llm_total = sum(llm_by_purpose.values())

    preflight_by_result = _aggregate_events(db, "image_preflight", "result", cutoff)

    variant_by_verdict = _aggregate_events(db, "variant_test", "verdict", cutoff)
    variant_by_accel = _aggregate_events(db, "variant_test", "accelerator", cutoff)
    variant_total = sum(variant_by_verdict.values())

    spec_queue = _latest_per_label(db, "speculative_queue", "accelerator", "depth")

    failed_runs = get_recent_failures(db, cutoff=cutoff, limit=limit)
    recent_failures = [_build_recent_failure(run, db) for run in failed_runs]

    return _WizardStatsResponse(
        extraction=_CounterBreakdown(
            total=extraction_total,
            by_label=extraction_by_result,
        ),
        extraction_by_mode=_CounterBreakdown(
            total=extraction_total,
            by_label=extraction_by_mode,
        ),
        repair=_RepairCounters(
            total=repair_total,
            infra_retry_count=infra_retry_count,
            by_outcome=repair_by_outcome,
            by_mode=repair_by_mode,
        ),
        llm_calls=_CounterBreakdown(
            total=llm_total,
            by_label=llm_by_purpose,
        ),
        image_preflight=_CounterBreakdown(
            total=sum(preflight_by_result.values()),
            by_label=preflight_by_result,
        ),
        variant_tests=_VariantTestCounters(
            total=variant_total,
            by_verdict=variant_by_verdict,
            by_accelerator=variant_by_accel,
        ),
        speculative_queue=spec_queue,
        recent_failures=recent_failures,
        window_hours=window_hours,
        fetched_at=datetime.now(timezone.utc),
    )
