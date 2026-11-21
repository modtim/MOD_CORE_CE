"""
Scheduler Admin Endpoints — observability for the predictive overlap scheduler.

Provides stats on prediction accuracy, pattern database, scheduling decisions,
and per-worker resource timelines.

clean-v2-api (admin dissolve wave): moved from `/admin/scheduler/*` to
`/system/scheduler/*` — no frontend caller found (grep-verified), so no
back-compat alias needed.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from services import scheduler_admin_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Clockwork Scheduler"])


class SchedulerStatsResponse(BaseModel):
    enabled: bool
    pattern_database: Dict[str, Any]
    task_queue: Dict[str, Any]
    scheduling: Dict[str, Any]
    workers: Dict[str, Any]


class WorkerTimelineResponse(BaseModel):
    error: Optional[str] = None
    worker_name: Optional[str] = None
    status: Optional[str] = None
    hardware: Optional[Dict[str, Any]] = None
    current_snapshot: Optional[Dict[str, Any]] = None
    active_tasks: Optional[List[Dict[str, Any]]] = None


class SchedulerPatternsResponse(BaseModel):
    patterns: List[Dict[str, Any]]


class PatternDetailResponse(BaseModel):
    error: Optional[str] = None
    node_kind: Optional[str] = None
    patterns: Optional[List[Dict[str, Any]]] = None


def require_platform_admin(current_user: models.User = Depends(require_active_and_onboarded_user)):
    """Dependency to ensure user is a platform admin."""
    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires platform admin privileges"
        )
    return current_user


@router.get("/system/scheduler/stats", response_model=SchedulerStatsResponse, response_model_exclude_unset=True)
async def get_scheduler_stats(
    _=Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    Overview stats for the predictive overlap scheduler.
    """
    return scheduler_admin_service.get_scheduler_stats(db)


@router.get(
    "/system/scheduler/timeline/{worker_name}",
    response_model=WorkerTimelineResponse,
    response_model_exclude_unset=True,
)
async def get_worker_timeline(
    worker_name: str,
    _=Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """
    Current projected resource timeline for a specific worker.
    Useful for debugging scheduling decisions.
    """
    return scheduler_admin_service.get_worker_timeline(db, worker_name)


@router.get("/system/scheduler/patterns", response_model=SchedulerPatternsResponse, response_model_exclude_unset=True)
async def list_patterns(
    _=Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """List all known resource patterns with confidence scores."""
    return scheduler_admin_service.list_patterns(db)


@router.get(
    "/system/scheduler/patterns/{node_kind}",
    response_model=PatternDetailResponse,
    response_model_exclude_unset=True,
)
async def get_pattern_detail(
    node_kind: str,
    _=Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """Detailed pattern for a specific node_kind with phase profiles."""
    return scheduler_admin_service.get_pattern_detail(db, node_kind)
