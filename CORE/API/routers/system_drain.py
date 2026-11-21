# API/routers/system_drain.py
"""GT.C2 — drain mode.

Surfaces and drives the platform-wide "stop dispatching, drain in-flight
work" switch used before any API restart (drain before
restart) and by CORE/startup/update.sh.

  - GET  /system/drain-status  — paused flag + in-flight run/task counts,
                                  excluding stale runs (no NodeRun progress
                                  for over 24h). `ready_for_restart` is true
                                  when paused AND both counts are 0.
  - POST /system/drain         — set dispatch.paused = True (pause).
  - POST /system/resume        — set dispatch.paused = False (resume).

The three routes are platform-admin only (mirrors routers/admin_gpu_pool.py's
gate). Writes go through the same DB-backed platform-settings service the
admin UI uses (services.platform_settings.set_setting), which already records
an append-only registry audit row; each write additionally emits the standard
admin-audit event (ADMIN_SYSTEM_MAINTENANCE — an existing value, so no new
enum/migration is needed).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

import audit
import models
from dependencies import get_db, require_active_and_onboarded_user
from services import platform_settings as psr

logger = logging.getLogger(__name__)

router = APIRouter(tags=["System - Drain"])
_impl = APIRouter()


def require_platform_admin(
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform admin only",
        )
    return current_user


#: A run with no NodeRun progress (started or completed) older than this is
#: treated as stale and excluded from the in-flight count so a genuinely
#: wedged run does not hold `ready_for_restart` hostage.
_STALE_CUTOFF_HOURS = 24


def _is_dispatch_paused(db: Session) -> bool:
    return bool(psr.get_setting("dispatch.paused", db=db))


def _drain_counts(db: Session) -> Dict[str, int]:
    """In-flight run/task counts.

    in_flight_runs  — WorkflowRun rows in a non-terminal state (queued /
                      running / pending_action), minus the stale ones.
    stale_runs_excluded — the excluded count (no NodeRun progress for over 24h).
    in_flight_tasks — api_task_queue rows not yet terminal (pending / assigned
                      / claimed / running).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=_STALE_CUTOFF_HOURS)
    row = db.execute(
        text(
            """
            WITH active_runs AS (
                SELECT wr.id AS run_id,
                       (SELECT COALESCE(max(nr.started_at), max(nr.completed_at))
                          FROM node_runs nr
                         WHERE nr.workflow_run_id = wr.id) AS last_progress
                FROM workflow_runs wr
                WHERE wr.status IN ('QUEUED', 'RUNNING', 'PENDING_ACTION')
                  AND wr.deleted_at IS NULL
            )
            SELECT
                (SELECT count(*) FROM active_runs
                  WHERE last_progress IS NOT NULL AND last_progress >= :cutoff)
                    AS in_flight_runs,
                (SELECT count(*) FROM active_runs
                  WHERE last_progress IS NULL OR last_progress < :cutoff)
                    AS stale_runs_excluded
            """
        ),
        {"cutoff": cutoff},
    ).one()
    in_flight_runs = int(row.in_flight_runs or 0)
    stale = int(row.stale_runs_excluded or 0)

    tasks_row = db.execute(
        text(
            """
            SELECT count(*) FROM api_task_queue
            WHERE status IN ('PENDING', 'ASSIGNED', 'CLAIMED', 'RUNNING')
            """
        )
    ).one()
    in_flight_tasks = int(tasks_row[0] or 0)
    return {
        "in_flight_runs": in_flight_runs,
        "in_flight_tasks": in_flight_tasks,
        "stale_runs_excluded": stale,
    }


def _build_status(db: Session) -> Dict[str, Any]:
    counts = _drain_counts(db)
    paused = _is_dispatch_paused(db)
    return {
        "paused": paused,
        "in_flight_runs": counts["in_flight_runs"],
        "in_flight_tasks": counts["in_flight_tasks"],
        "stale_runs_excluded": counts["stale_runs_excluded"],
        "ready_for_restart": bool(
            paused
            and counts["in_flight_runs"] == 0
            and counts["in_flight_tasks"] == 0,
        ),
    }


class DrainResponse(BaseModel):
    key: str
    value: bool
    message: str


def _set_dispatch_paused(
    db: Session,
    admin: models.User,
    paused: bool,
    request: Request,
) -> DrainResponse:
    """Flip dispatch.paused through the settings service (soft-supersedes +
    append-only registry audit), then emit the standard admin-audit event.
    `paused` is the target state; `maintenance_type` distinguishes the two."""
    old_value = psr.get_setting("dispatch.paused", db=db)
    psr.set_setting(
        "dispatch.paused", paused, db=db, actor_user_id=admin.id,
    )
    try:
        audit.log_admin_action(
            db,
            models.AuditAction.ADMIN_SYSTEM_MAINTENANCE,
            admin.id,
            request=request,
            resource_type="system",
            metadata={
                "maintenance_type": "dispatch_drain" if paused
                                    else "dispatch_resume",
                "key": "dispatch.paused",
                "old_value": old_value,
                "new_value": paused,
            },
        )
    except Exception:  # noqa: BLE001 — never mask the mutation on an audit
        logger.warning("drain/resume admin audit emit failed (non-fatal)",
                       exc_info=True)
    db.commit()
    msg = ("Dispatch paused: no new claims/dispatch; in-flight work is "
           "draining." if paused else
           "Dispatch resumed: new work may be claimed.")
    return DrainResponse(key="dispatch.paused", value=paused, message=msg)


class DrainStatusResponse(BaseModel):
    paused: bool
    in_flight_runs: int
    in_flight_tasks: int
    stale_runs_excluded: int
    ready_for_restart: bool


@_impl.get("/drain-status", response_model=DrainStatusResponse)
def drain_status(
    db: Session = Depends(get_db),
    _admin: models.User = Depends(require_platform_admin),
) -> Dict[str, Any]:
    """Current drain state + in-flight counts (stale runs excluded)."""
    return _build_status(db)


@_impl.post("/drain", response_model=DrainResponse)
def drain(
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_platform_admin),
) -> DrainResponse:
    """Pause dispatch (GT.C2). Idempotent — pausing an already-paused
    platform just rewrites the same value."""
    return _set_dispatch_paused(db, admin, True, request)


@_impl.post("/resume", response_model=DrainResponse)
def resume(
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_platform_admin),
) -> DrainResponse:
    """Resume dispatch (GT.C2)."""
    return _set_dispatch_paused(db, admin, False, request)


router.include_router(_impl, prefix="/system")
