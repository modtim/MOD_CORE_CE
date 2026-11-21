"""
Per-device accelerator profiles + benchmark strategy admin endpoints.

Serves the Dashboard's Accelerators panel in the worker detail modal and
the admin review UI for LLM-generated strategies.
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from dependencies import get_db, get_current_user
from models import User
from typing import Optional
import logging

from services import accelerators_service
from services._base import ServiceError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/accelerators", tags=["accelerators"])


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer `ServiceError` to the `HTTPException` this router
    always raised directly before the Gate 2b migration (same status/detail
    shape — a plain string, not the (not-yet-installed) error envelope)."""
    status = {"forbidden": 403, "not_found": 404, "bad_request": 400}.get(exc.code, 400)
    raise HTTPException(status_code=status, detail=exc.message)


class WorkerAcceleratorsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    worker_name: Optional[str] = None
    hardware_profile_id: Optional[int] = None
    profiling_status: Optional[str] = None
    progress: Optional[dict] = None
    system_benchmark: Optional[dict] = None
    accelerators: Optional[list] = None


@router.get("/worker/{worker_name}", response_model=WorkerAcceleratorsResponse)
async def get_worker_accelerators(
    worker_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Per-device accelerator profiles for a worker. Dashboard detail modal."""
    try:
        return accelerators_service.get_worker_accelerators(db, worker_name, current_user)
    except ServiceError as exc:
        _raise_http(exc)


class AcceleratorEnabledRequest(BaseModel):
    enabled: bool
    reason: Optional[str] = None


class AcceleratorEnabledResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    success: Optional[bool] = None
    accelerator_id: Optional[int] = None
    kind: Optional[str] = None
    index: Optional[str] = None
    admin_disabled: Optional[bool] = None
    benchmark_status: Optional[str] = None
    message: Optional[str] = None


@router.post("/{accelerator_id}/set-enabled", response_model=AcceleratorEnabledResponse)
async def set_accelerator_enabled(
    accelerator_id: int,
    body: AcceleratorEnabledRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One-click operator quarantine — disable/enable a single accelerator.

    Disabling excludes the device from scheduling (the claim path drops its
    queue when EVERY device of that kind is dead) WITHOUT re-benchmarking or
    touching its scores; enabling clears the flag. The worker stays fully
    healthy on its remaining accelerators and CPU either way.

    Dashboard binding: worker detail modal → Accelerators panel → each device
    card gets a "Disable"/"Enable" toggle that POSTs here with {enabled}. The
    card's badge reads `admin_disabled` (shown as "Disabled by operator") in
    addition to `benchmark_status`.
    """
    try:
        return accelerators_service.set_accelerator_enabled(db, accelerator_id, body, current_user)
    except ServiceError as exc:
        _raise_http(exc)


class StrategyResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: Optional[int] = None
    kind: Optional[str] = None
    vendor_id: Optional[str] = None
    device_id_pattern: Optional[str] = None
    device_family: Optional[str] = None
    image_ref: Optional[str] = None
    source: Optional[str] = None
    auto_approved: Optional[bool] = None
    runs_succeeded: Optional[int] = None
    runs_failed: Optional[int] = None
    last_success_at: Optional[str] = None
    last_anomaly_at: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None


class ListStrategiesResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    strategies: Optional[list] = None


@router.get("/strategies", response_model=ListStrategiesResponse)
async def list_strategies(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all benchmark strategies. Admin review UI."""
    try:
        return accelerators_service.list_strategies(db, current_user)
    except ServiceError as exc:
        _raise_http(exc)


class StrategyActionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    success: Optional[bool] = None
    message: Optional[str] = None


@router.post("/strategies/{strategy_id}/approve", response_model=StrategyActionResponse)
async def approve_strategy(
    strategy_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Manually approve an LLM-generated strategy."""
    try:
        return accelerators_service.approve_strategy(db, strategy_id, current_user)
    except ServiceError as exc:
        _raise_http(exc)


@router.post("/strategies/{strategy_id}/reject", response_model=StrategyActionResponse)
async def reject_strategy(
    strategy_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reject and delete an LLM-generated strategy. Seeded strategies can't be rejected."""
    try:
        return accelerators_service.reject_strategy(db, strategy_id, current_user)
    except ServiceError as exc:
        _raise_http(exc)
