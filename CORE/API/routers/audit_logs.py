# API/routers/audit_logs.py

from typing import List, Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime
import logging

import schemas
import models
from dependencies import (get_db, require_active_and_onboarded_user,
                          require_auditor_or_admin)
from services import audit_logs_service
from services._base import NotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/audit-logs",
    tags=["Audit Logs"],
    dependencies=[Depends(require_active_and_onboarded_user)],
)


@router.get("/permissions", response_model=List[schemas.PermissionAuditLog])
def get_permission_audit_logs(
    resource_type: Optional[str] = Query(None, description="Filter by resource type (e.g., 'cog')"),
    resource_id: Optional[int] = Query(None, description="Filter by specific resource ID"),
    action: Optional[str] = Query(None, description="Filter by action type"),
    actor_user_id: Optional[int] = Query(None, description="Filter by who performed the action"),
    target_user_id: Optional[int] = Query(None, description="Filter by who was affected"),
    start_date: Optional[datetime] = Query(None, description="Filter logs after this date"),
    end_date: Optional[datetime] = Query(None, description="Filter logs before this date"),
    limit: int = Query(100, le=1000, description="Maximum number of logs to return"),
    offset: int = Query(0, ge=0, description="Number of logs to skip"),
    q: Optional[str] = Query(
        None,
        max_length=200,
        description="Free-text search: action, resource type, actor/target email",
    ),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Retrieve permission audit logs with optional filtering.

    Returns audit logs for the current user's tenant, ordered by most recent first.
    """
    return audit_logs_service.list_permission_audit_logs(
        db,
        current_user,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        actor_user_id=actor_user_id,
        target_user_id=target_user_id,
        start_date=start_date,
        end_date=end_date,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/permissions/{log_id}", response_model=schemas.PermissionAuditLog)
def get_permission_audit_log(
    log_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Retrieve a specific permission audit log entry."""
    try:
        return audit_logs_service.get_permission_audit_log(db, current_user, log_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)


@router.get("/export")
def export_audit_logs(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_auditor_or_admin),
):
    """
    Export audit logs as plain JSON (permission audit logs and general audit
    events). The export has no integrity hashes and no verification result.
    """
    export_data = audit_logs_service.build_audit_export(
        db, current_user, start_date=start_date, end_date=end_date
    )
    return JSONResponse(content=export_data)
