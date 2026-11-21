"""Platform-admin endpoints for the scheduler runtime config.

Two endpoints:

- GET  /platform/scheduler-runtime-config — return the currently
  effective config plus a `source` field ('db' | 'env' | 'default').
  Lazy-resolves from env/defaults if the table is empty; does NOT
  insert a row.

- POST /platform/scheduler-runtime-config — upsert the singleton row,
  emit a SCHEDULER_CONFIG_CHANGED audit event, refresh the in-process
  cache so the next packer tick reads the new values, return the
  persisted row.

Both endpoints are gated to platform admins only. Server-side preset
detection means the client can't claim a preset name that doesn't
match the supplied slider values.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

import models
import services.scheduler_runtime_config_service as svc
from dependencies import get_db, require_active_and_onboarded_user
from services import scheduler_runtime_cache as cache

logger = logging.getLogger(__name__)

SCHEDULER_CONFIG_CHANGED_EVENT = "SCHEDULER_CONFIG_CHANGED"

router = APIRouter(
    prefix="/platform/scheduler-runtime-config",
    tags=["scheduler-runtime-config"],
)


# -- auth ------------------------------------------------------------------


def _require_platform_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform admin required",
        )
    return current_user


# -- schemas ---------------------------------------------------------------


class SchedulerRuntimeConfigResponse(BaseModel):
    preset_name: str
    capacity_floor_pct: int
    packer_full_above_util_pct: int
    variance_penalty_s: int
    background_max_fleet_pct: int
    source: str  # 'db' | 'env' | 'default'


class SchedulerRuntimeConfigUpdate(BaseModel):
    capacity_floor_pct: int = Field(..., ge=0, le=90)
    packer_full_above_util_pct: int = Field(..., ge=10, le=100)
    variance_penalty_s: int = Field(..., ge=1, le=60)
    background_max_fleet_pct: int = Field(..., ge=5, le=90)
    # Optional: client may suggest a preset name, but the server
    # always re-derives from the slider values to avoid mislabelling.
    preset_name: Optional[str] = Field(default=None, max_length=20)

    @field_validator("preset_name")
    @classmethod
    def _allowed_preset(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ("aggressive", "normal", "light", "custom"):
            raise ValueError("preset_name must be one of aggressive/normal/light/custom")
        return v


# -- helpers ---------------------------------------------------------------


def _snapshot_to_response(snap: cache.SchedulerRuntimeConfigSnapshot) -> SchedulerRuntimeConfigResponse:
    return SchedulerRuntimeConfigResponse(
        preset_name=snap.preset_name,
        capacity_floor_pct=snap.capacity_floor_pct,
        packer_full_above_util_pct=snap.packer_full_above_util_pct,
        variance_penalty_s=snap.variance_penalty_s,
        background_max_fleet_pct=snap.background_max_fleet_pct,
        source=snap.source,
    )


# -- endpoints -------------------------------------------------------------


@router.get("", response_model=SchedulerRuntimeConfigResponse)
def get_scheduler_runtime_config(
    db: Session = Depends(get_db),
    _admin: models.User = Depends(_require_platform_admin),
) -> SchedulerRuntimeConfigResponse:
    """Return the currently-effective scheduler runtime config.

    Resolution order: DB row → env var → in-code defaults. Source field
    surfaces which path won. Empty table is NOT lazily seeded by GET.
    """
    snap = svc.get_effective_config(db)
    return _snapshot_to_response(snap)


@router.post("", response_model=SchedulerRuntimeConfigResponse)
def update_scheduler_runtime_config(
    payload: SchedulerRuntimeConfigUpdate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(_require_platform_admin),
) -> SchedulerRuntimeConfigResponse:
    """Upsert the singleton config row, emit audit, refresh cache."""
    snap = svc.update_config(
        db,
        admin=admin,
        capacity_floor_pct=payload.capacity_floor_pct,
        packer_full_above_util_pct=payload.packer_full_above_util_pct,
        variance_penalty_s=payload.variance_penalty_s,
        background_max_fleet_pct=payload.background_max_fleet_pct,
    )
    return _snapshot_to_response(snap)
