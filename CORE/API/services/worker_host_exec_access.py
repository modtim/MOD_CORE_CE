"""services/worker_host_exec_access.py -- hx.15b (opsx worker-host-exec,
D2): admin API backing for per-worker host-exec access policy + grants.

Policy + grant rows live in models.WorkerHostExecPolicy /
models.WorkerHostExecGrant (hx.15, migration 20261010j). This module only
decides who may manage them and mutates them; the enforcement side stays
in services/worker_host_exec_service.py (`_worker_access_policy`, owned by
hx.5).

Manager rules (tasks.md D2):
  * platform admin -> may manage any worker;
  * owning-tenant root admin -> may manage DEDICATED (tenant-owned) workers
    only (svc._is_dedicated);
  * everyone else -> 403.
Shared / PLATFORM_POOL workers are always sandboxed (D2): platform admin
only, policy must stay 'admins', and grants with allow_root=true are
rejected with 400 (root is never allowed on a shared worker).

ISO 27001: no hard delete. A policy change soft-deletes the previous row
(`deleted_at`) and appends a new one; a revoke sets `revoked_at`
(+ `revoked_by_user_id`) and never deletes. Every change writes an
AuditEvent (ADMIN_WORKER_MODIFY + audit_metadata host_exec_action).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import audit
import models
from services import worker_host_exec_service as svc

from product_names import themed_logger

logger = themed_logger(__name__, 'workers')

ACCESS_VALUES = ("admins", "grants", "all_members")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_manager(user: "models.User", worker: "models.WorkerLicense") -> bool:
    """Who may manage a worker's host-exec access policy/grants."""
    if bool(getattr(user, "is_platform_admin", False)):
        return True
    if not svc._is_dedicated(worker):
        return False
    return (
        getattr(user, "tenant_id", None) == worker.owner_tenant_id
        and svc._is_tenant_admin(user)
    )


def require_manager(user: "models.User", worker: "models.WorkerLicense") -> None:
    if not _is_manager(user, worker):
        raise HTTPException(
            status_code=403,
            detail="only a platform admin or the owning tenant's admin may "
                   "manage host-exec access for this worker",
        )


def get_active_policy(db: Session, worker: "models.WorkerLicense"):
    """The worker's current (non soft-deleted) policy row, or None."""
    return (
        db.query(models.WorkerHostExecPolicy)
        .filter(
            models.WorkerHostExecPolicy.worker_id == worker.id,
            models.WorkerHostExecPolicy.deleted_at.is_(None),
        )
        .order_by(models.WorkerHostExecPolicy.created_at.desc())
        .first()
    )


def list_active_grants(
    db: Session, worker: "models.WorkerLicense"
) -> List["models.WorkerHostExecGrant"]:
    return (
        db.query(models.WorkerHostExecGrant)
        .filter(
            models.WorkerHostExecGrant.worker_id == worker.id,
            models.WorkerHostExecGrant.revoked_at.is_(None),
        )
        .order_by(models.WorkerHostExecGrant.created_at)
        .all()
    )


def get_policy(db: Session, worker: "models.WorkerLicense") -> str:
    """Effective access value ('admins' when the worker has no policy row)."""
    policy = get_active_policy(db, worker)
    return policy.access if policy is not None else "admins"


def set_policy(
    db: Session,
    user: "models.User",
    worker: "models.WorkerLicense",
    access: str,
    request: Optional["Any"] = None,
) -> "models.WorkerHostExecPolicy":
    """Change the worker's access policy: soft-delete the current row and
    append a new one (history kept). Shared workers stay 'admins'."""
    require_manager(user, worker)
    if access not in ACCESS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"access must be one of {ACCESS_VALUES}",
        )
    if not svc._is_dedicated(worker) and access != "admins":
        raise HTTPException(
            status_code=400,
            detail="shared/platform-pool workers are always sandboxed -- "
                   "their host-exec access policy must stay 'admins'",
        )

    current = get_active_policy(db, worker)
    if current is not None:
        current.deleted_at = _utcnow()
    new_policy = models.WorkerHostExecPolicy(
        worker_id=worker.id,
        access=access,
        updated_by_user_id=getattr(user, "id", None),
    )
    db.add(new_policy)
    db.commit()
    db.refresh(new_policy)

    audit.log_security_event(
        db,
        models.AuditAction.ADMIN_WORKER_MODIFY,
        request=request,
        user_id=getattr(user, "id", None),
        tenant_id=worker.owner_tenant_id,
        metadata={
            "host_exec_action": "policy_set",
            "worker_id": worker.id,
            "policy_id": new_policy.id,
            "access": access,
            "previous_access": current.access if current is not None else None,
        },
    )
    return new_policy


def create_grant(
    db: Session,
    user: "models.User",
    worker: "models.WorkerLicense",
    grantee_user_id: int,
    allow_root: bool = False,
    request: Optional["Any"] = None,
) -> "models.WorkerHostExecGrant":
    require_manager(user, worker)
    dedicated = svc._is_dedicated(worker)
    if not dedicated and allow_root:
        raise HTTPException(
            status_code=400,
            detail="root host-exec is never granted on a shared worker",
        )

    grantee = db.get(models.User, grantee_user_id)
    if grantee is None or getattr(grantee, "status", None) != models.UserStatus.ACTIVE:
        raise HTTPException(status_code=404, detail="grantee user not found")
    if dedicated and getattr(grantee, "tenant_id", None) != worker.owner_tenant_id:
        raise HTTPException(
            status_code=400,
            detail="grantee must be a member of the worker's owning tenant",
        )

    existing = [g for g in list_active_grants(db, worker)
                if g.grantee_user_id == grantee_user_id]
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"an active grant already exists for user {grantee_user_id} "
                   "on this worker",
        )

    grant = models.WorkerHostExecGrant(
        worker_id=worker.id,
        grantee_user_id=grantee_user_id,
        granted_by_user_id=getattr(user, "id", None),
        allow_root=bool(allow_root),
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)

    audit.log_security_event(
        db,
        models.AuditAction.ADMIN_WORKER_MODIFY,
        request=request,
        user_id=getattr(user, "id", None),
        tenant_id=worker.owner_tenant_id,
        metadata={
            "host_exec_action": "grant_create",
            "worker_id": worker.id,
            "grant_id": grant.id,
            "grantee_user_id": grantee_user_id,
            "allow_root": grant.allow_root,
        },
    )
    return grant


def revoke_grant(
    db: Session,
    user: "models.User",
    worker: "models.WorkerLicense",
    grant_id: int,
    request: Optional["Any"] = None,
) -> "models.WorkerHostExecGrant":
    """Revoke = set revoked_at/revoked_by (never delete the row)."""
    require_manager(user, worker)
    grant = db.get(models.WorkerHostExecGrant, grant_id)
    if grant is None or grant.worker_id != worker.id:
        raise HTTPException(status_code=404, detail="grant not found")
    if grant.revoked_at is not None:
        raise HTTPException(status_code=409, detail="grant is already revoked")

    grant.revoked_at = _utcnow()
    grant.revoked_by_user_id = getattr(user, "id", None)
    db.commit()
    db.refresh(grant)

    audit.log_security_event(
        db,
        models.AuditAction.ADMIN_WORKER_MODIFY,
        request=request,
        user_id=getattr(user, "id", None),
        tenant_id=worker.owner_tenant_id,
        metadata={
            "host_exec_action": "grant_revoke",
            "worker_id": worker.id,
            "grant_id": grant.id,
            "grantee_user_id": grant.grantee_user_id,
        },
    )
    return grant


def get_access_summary(
    db: Session, worker: "models.WorkerLicense"
) -> Dict[str, Any]:
    policy = get_active_policy(db, worker)
    grants = list_active_grants(db, worker)
    return {
        "access": policy.access if policy is not None else "admins",
        "grants": [
            {
                "id": g.id,
                "grantee_user_id": g.grantee_user_id,
                "allow_root": bool(g.allow_root),
                "granted_by": g.granted_by_user_id,
                "created_at": g.created_at.isoformat() if g.created_at else None,
            }
            for g in grants
        ],
    }
