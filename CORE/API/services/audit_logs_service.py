# API/services/audit_logs_service.py
"""Business logic for the audit-logs domain (routers/audit_logs.py).

Community Edition: an admin reads their own audit log (list and detail) and
downloads it as a plain JSON export. The export carries the events as
recorded; it has no integrity hashes, no previous-hash chain and no
verification result.

`crud/audit_logs.py` is the only place `db.query(models.X)` runs for this
domain.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models
import schemas
from crud import audit_logs as audit_logs_crud
from services._base import NotFoundError


def _enrich_permission_audit_log(
    log: "models.PermissionAuditLog", cog_map: Dict[int, Dict[str, Any]]
) -> schemas.PermissionAuditLog:
    cog_info = cog_map.get(log.resource_id, {}) if log.resource_type == "cog" else {}
    return schemas.PermissionAuditLog(
        id=log.id,
        tenant_id=log.tenant_id,
        action=log.action.value,
        timestamp=log.timestamp,
        actor_user_id=log.actor_user_id,
        target_user_id=log.target_user_id,
        resource_type=log.resource_type,
        resource_id=log.resource_id,
        permission_level=log.permission_level,
        permission_id=log.permission_id,
        permission_request_id=log.permission_request_id,
        metadata=log.audit_metadata,
        ip_address=log.ip_address,
        user_agent=log.user_agent,
        actor_email=log.actor.email if log.actor else None,
        target_email=log.target_user.email if log.target_user else None,
        resource_name=cog_info.get("name"),
        parent_name=cog_info.get("parent_name"),
    )


def list_permission_audit_logs(
    db: Session,
    current_user: "models.User",
    *,
    resource_type: Optional[str] = None,
    resource_id: Optional[int] = None,
    action: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    target_user_id: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    q: Optional[str] = None,
    limit: int,
    offset: int,
) -> List[schemas.PermissionAuditLog]:
    """List the caller's tenant's permission audit logs, enriched with
    actor/target email and (for `resource_type == "cog"`) resource + parent
    names.

    `q` is the free-text search bar: case-insensitive match on the action, the
    resource type, or the actor / target email.
    """
    logs = audit_logs_crud.list_permission_audit_logs(
        db,
        tenant_id=current_user.tenant_id,
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

    cog_ids = {log.resource_id for log in logs if log.resource_type == "cog"}
    cog_map = audit_logs_crud.get_cog_name_map(db, cog_ids)

    return [_enrich_permission_audit_log(log, cog_map) for log in logs]


def get_permission_audit_log(
    db: Session, current_user: "models.User", log_id: int
) -> schemas.PermissionAuditLog:
    """Fetch a single tenant-scoped permission audit log entry. Raises
    `NotFoundError` when absent (router maps -> 404)."""
    log = audit_logs_crud.get_permission_audit_log(
        db, log_id, tenant_id=current_user.tenant_id
    )
    if not log:
        raise NotFoundError("Audit log not found")

    return schemas.PermissionAuditLog(
        id=log.id,
        tenant_id=log.tenant_id,
        action=log.action.value,
        timestamp=log.timestamp,
        actor_user_id=log.actor_user_id,
        target_user_id=log.target_user_id,
        resource_type=log.resource_type,
        resource_id=log.resource_id,
        permission_level=log.permission_level,
        permission_id=log.permission_id,
        permission_request_id=log.permission_request_id,
        metadata=log.audit_metadata,
        ip_address=log.ip_address,
        user_agent=log.user_agent,
        actor_email=log.actor.email if log.actor else None,
        target_email=log.target_user.email if log.target_user else None,
    )


def build_audit_export(
    db: Session,
    current_user: "models.User",
    *,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Assemble the plain JSON export document: permission audit logs plus
    general audit events. No integrity hash, previous hash or verification
    result is included."""
    perm_logs = audit_logs_crud.list_permission_audit_logs_for_export(
        db, start_date=start_date, end_date=end_date
    )
    audit_events = audit_logs_crud.list_audit_events_for_export(
        db, start_date=start_date, end_date=end_date
    )

    export_data: Dict[str, Any] = {
        "export_metadata": {
            "exported_at": datetime.utcnow().isoformat(),
            "exported_by_user_id": current_user.id,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        "permission_audit_logs": [],
        "audit_events": [],
    }

    for log in perm_logs:
        export_data["permission_audit_logs"].append({
            "id": log.id,
            "tenant_id": log.tenant_id,
            "action": log.action.value if log.action else None,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "actor_user_id": log.actor_user_id,
            "target_user_id": log.target_user_id,
            "resource_type": log.resource_type,
            "resource_id": log.resource_id,
            "metadata": log.audit_metadata,
        })

    for event in audit_events:
        export_data["audit_events"].append({
            "id": event.id,
            "tenant_id": event.tenant_id,
            "user_id": event.user_id,
            "event_name": event.event_name,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "details": event.details,
        })

    return export_data
