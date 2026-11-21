"""
Training data generation endpoint.

One button, one click: dispatches test workflows across all online workers
to collect resource samples for the TFT scheduling model.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from dependencies import get_db, get_current_user
from models import User
from services import training_service
from services._base import NotFoundError
from services.training_service import BadRequestError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/platform/training", tags=["training"])


class GenerateTrainingDataResponse(BaseModel):
    dispatched: int
    errors: int
    workers: int
    worker_names: List[str]
    workflows_used: List[str]
    cogs_used: int
    estimated_hours: float
    message: str


class TrainingDataStatusResponse(BaseModel):
    resource_samples: int
    resource_patterns: int
    completed_workflow_tasks: int
    training_runs_dispatched: int
    training_runs_completed: int
    unique_dag_types_with_patterns: int
    profiled_workers: int
    ready_for_training: bool
    recommendation: str


class GenerateRequest(BaseModel):
    runs_per_worker: int = Field(default=500, ge=1, le=10000)
    workflow_version_id: Optional[int] = None
    max_concurrent: int = Field(default=20, ge=1, le=100)


@router.post("/generate", response_model=GenerateTrainingDataResponse)
async def generate_training_data(
    request: GenerateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Dispatch test workflows across all online workers to collect resource
    profiling data for the TFT scheduling model.

    Uses real workflow execution — same pipeline as user-triggered runs.
    Resource samples are collected automatically by the worker monitor.
    Pattern updater fires on each completion.

    Platform admin only.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    try:
        return training_service.generate_training_data(
            db, tenant_id=current_user.tenant_id, request=request,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)
    except BadRequestError as exc:
        raise HTTPException(status_code=400, detail=exc.message)


@router.get("/status", response_model=TrainingDataStatusResponse)
async def training_data_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check how much training data has been collected."""
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin only")

    return training_service.training_data_status(db)
