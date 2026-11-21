# In Main/routers/locks.py

from typing import List, Optional
from fastapi import APIRouter, Depends, status, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

import schemas, crud, models, audit
from dependencies import get_db
from dependencies import require_active_and_onboarded_user

router = APIRouter(
    tags=["Resource Locks"],
    dependencies=[Depends(require_active_and_onboarded_user)]
)


def _can_force_unlock(user: models.User) -> bool:
    """Who may forcibly release a lock held by someone else.

    - Platform admins (god-mode) always may (cross-tenant).
    - A tenant's "Admin" system-role holder may, scoped to their OWN tenant
      (enforced by the caller). There is no distinct "Owner" role in this
      codebase — the tenant owner is the Admin-role holder.
    """
    if getattr(user, "is_platform_admin", False):
        return True
    role = getattr(user, "custom_role", None)
    if role is not None and getattr(role, "name", None) == "Admin":
        return True
    return False


@router.get("/", response_model=List[schemas.ResourceLock])
def list_active_locks(
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user),
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "expires_at",
        order: str = "asc"
):
    """
    Lists all active resource locks for the current tenant.
    This is useful for administrative oversight.
    """
    return crud.get_active_locks_for_tenant(
        db=db, tenant_id=current_user.tenant_id, limit=limit, offset=offset, sort_by=sort_by, order=order
    )


@router.get("/{target_type}/{target_id}", response_model=Optional[schemas.ResourceLock])
def get_lock_status(
    target_type: str,
    target_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """
    Retrieves the details of a lock on a specific resource, or ``null`` when
    the resource is unlocked (the normal case — not an error). Scoped to the
    caller's tenant so another tenant's lock holder is never disclosed.
    """
    lock = crud.get_active_lock_for_resource(db, target_type=target_type, target_id=target_id)
    if not lock:
        return None
    if not getattr(current_user, "is_platform_admin", False) and lock.tenant_id != current_user.tenant_id:
        return None
    return lock


@router.post("/", response_model=schemas.ResourceLock, status_code=status.HTTP_201_CREATED)
def acquire_lock(
        lock: schemas.ResourceLockCreate,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Attempts to acquire a lock on a resource for the current user.
    If the resource is already locked, it returns a 409 Conflict.
    """
    # First, explicitly check if a lock exists to provide a clear error message
    existing_lock = crud.get_active_lock_for_resource(db, lock.target_type, lock.target_id)
    if existing_lock:
        detail = "Resource is already locked."
        if existing_lock.user_id == current_user.id:
            detail = "You already have a lock on this resource."
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        )

    # The UniqueConstraint on the model provides atomicity.
    # We wrap the creation in a try/except to handle the race condition
    # where a lock is created between the check above and this call.
    try:
        return crud.acquire_lock(db=db, lock=lock, user=current_user)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource was locked by another process just now. Please try again.",
        )


@router.post("/{target_type}/{target_id}/renew", response_model=schemas.ResourceLock)
def renew_lock(
        target_type: str,
        target_id: int,
        payload: Optional[schemas.ResourceLockRenew] = None,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Renew/extend a lock the current user holds, pushing its expiry out by the
    configured TTL. Returns 403 if the user does not hold an active lock on the
    resource (they must re-acquire, which re-checks for a competing holder).
    """
    payload = payload or schemas.ResourceLockRenew()
    renewed = crud.renew_lock(
        db,
        target_type=target_type,
        target_id=target_id,
        user_id=current_user.id,
        ttl_minutes=payload.ttl_minutes,
        reason=payload.reason,
    )
    if not renewed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active lock held by you on this resource to renew.",
        )
    return renewed


@router.delete("/{target_type}/{target_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def release_lock(
        target_type: str,
        target_id: int,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Releases a lock on a resource that was held by the current user.
    If the user does not hold the lock, it will return a 403 Forbidden.
    """
    lock_released = crud.release_lock(db, target_type=target_type, target_id=target_id, user_id=current_user.id)
    if not lock_released:
        raise HTTPException(status_code=403, detail="No active lock found for this resource held by the current user.")
    return


@router.delete("/{target_type}/{target_id}/force", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def force_release_lock(
        target_type: str,
        target_id: int,
        request: Request,
        db: Session = Depends(get_db),
        current_user: models.User = Depends(require_active_and_onboarded_user)
):
    """
    Administratively release a lock held by ANOTHER user (platform admin, or a
    tenant Admin within their own tenant). Audited as a security override.
    Returns 403 if the caller is not permitted, 404 if no active lock exists.
    """
    if not _can_force_unlock(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to force-unlock this resource.",
        )

    existing = crud.get_active_lock_for_resource(db, target_type=target_type, target_id=target_id)
    if not existing:
        raise HTTPException(status_code=404, detail="No active lock found for this resource.")

    # Tenant Admins (non-platform) may only force-unlock within their own tenant.
    if not getattr(current_user, "is_platform_admin", False):
        if existing.tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You may only force-unlock resources within your own tenant.",
            )

    snapshot = crud.force_release_lock(db, target_type=target_type, target_id=target_id)
    if snapshot is None:
        # Lost a race — someone released it between the check and the delete.
        raise HTTPException(status_code=404, detail="No active lock found for this resource.")

    # Audit the override (CRITICAL). Never let an audit failure block the release.
    try:
        audit.log_admin_security_override(
            db=db,
            admin_user_id=current_user.id,
            override_type="resource_lock_force_release",
            request=request,
            target_tenant_id=snapshot.get("tenant_id"),
            target_user_id=snapshot.get("holder_user_id"),
            reason=snapshot.get("reason"),
            metadata={
                "lock_id": snapshot.get("lock_id"),
                "target_type": snapshot.get("target_type"),
                "target_id": snapshot.get("target_id"),
                "original_expires_at": snapshot.get("expires_at"),
            },
        )
    except Exception:
        pass
    return
