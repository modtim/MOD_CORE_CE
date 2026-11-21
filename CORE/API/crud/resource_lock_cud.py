"""Resource Lock CRUD operations - Resource locking within tenants."""
import os
from typing import Optional, List

from sqlalchemy import or_
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

import models, schemas
from sqlalchemy.exc import IntegrityError


# A deliberate, user-initiated lock HOLDS UNTIL EXPLICITLY UNLOCKED by default —
# no countdown, no renew burden. `expires_at IS NULL` means "no expiry"; the
# active-lock queries below treat a NULL expiry as still-active. The admin
# force-unlock path is the only stale-recovery mechanism for an abandoned lock.
#
# A timed lock remains available as an OPT-IN: pass `ttl_minutes` on the
# acquire request, or set the RESOURCE_LOCK_TTL_MINUTES env var to a positive
# integer to make every lock timed by default. When neither is supplied the
# lock never auto-expires. An absolute ceiling caps any caller-supplied TTL so
# a client can't pin a timed lock for an unreasonable span.
_MAX_TTL_MINUTES = 60 * 24 * 30     # 30 days hard ceiling


def _env_ttl_minutes() -> Optional[int]:
    """The RESOURCE_LOCK_TTL_MINUTES override, or None when unset/invalid/<=0.

    Returning None is what makes the DEFAULT a no-expiry lock: only an
    explicitly-configured positive value opts the deployment into timed locks.
    """
    raw = os.getenv("RESOURCE_LOCK_TTL_MINUTES")
    if raw is None:
        return None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    return min(val, _MAX_TTL_MINUTES)


def default_ttl_minutes() -> int:
    """Configured default TTL used by the (optional) renew path, in minutes.

    Falls back to a sane 8h window when no env override is set — renew is only
    meaningful for timed locks, and a caller who renews wants a bounded push-out
    rather than an unbounded one.
    """
    env = _env_ttl_minutes()
    return env if env is not None else 480


def _resolve_ttl_minutes(requested: Optional[int]) -> int:
    """Clamp a caller-supplied TTL into (0, _MAX_TTL_MINUTES]; fall back to the
    configured default when None/invalid. Used by the renew path only."""
    if requested is None:
        return default_ttl_minutes()
    try:
        val = int(requested)
    except (TypeError, ValueError):
        return default_ttl_minutes()
    if val <= 0:
        return default_ttl_minutes()
    return min(val, _MAX_TTL_MINUTES)


def _resolve_expiry(requested_ttl: Optional[int]) -> Optional[datetime]:
    """Compute the expires_at for a NEW lock.

    Precedence: an explicit per-request ``ttl_minutes`` wins; otherwise the
    RESOURCE_LOCK_TTL_MINUTES env override; otherwise None (no expiry — the
    lock holds until explicitly unlocked). A non-positive/invalid per-request
    ttl is ignored (falls through to env, then no-expiry).
    """
    if requested_ttl is not None:
        try:
            val = int(requested_ttl)
        except (TypeError, ValueError):
            val = 0
        if val > 0:
            return datetime.utcnow() + timedelta(minutes=min(val, _MAX_TTL_MINUTES))
    env = _env_ttl_minutes()
    if env is not None:
        return datetime.utcnow() + timedelta(minutes=env)
    return None


def _active_expiry_clause():
    """SQLAlchemy predicate: a lock is active when it has no expiry (NULL) or
    its expiry is still in the future."""
    return or_(
        models.ResourceLock.expires_at.is_(None),
        models.ResourceLock.expires_at > datetime.utcnow(),
    )


def get_active_locks_for_tenant(
    db: Session,
    tenant_id: int,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "expires_at",
    order: str = "asc",
) -> List[models.ResourceLock]:
    """Retrieve all active (non-expired) resource locks for a specific tenant."""
    query = db.query(models.ResourceLock).filter(
        models.ResourceLock.tenant_id == tenant_id,
        _active_expiry_clause(),
    )

    if hasattr(models.ResourceLock, sort_by):
        column = getattr(models.ResourceLock, sort_by)
        if order.lower() == "desc":
            query = query.order_by(column.desc())
        else:
            query = query.order_by(column.asc())

    return query.offset(offset).limit(limit).all()


def get_active_lock_for_resource(
    db: Session,
    target_type: str,
    target_id: int
) -> Optional[models.ResourceLock]:
    """Retrieve the active lock for a specific resource, if one exists."""
    return (
        db.query(models.ResourceLock)
        .filter(
            models.ResourceLock.target_type == target_type,
            models.ResourceLock.target_id == target_id,
            _active_expiry_clause(),
        )
        .first()
    )


def acquire_lock(
    db: Session,
    lock: schemas.ResourceLockCreate,
    user: models.User
) -> models.ResourceLock:
    """Attempts to acquire a lock on a resource for a user.

    By default the lock has NO expiry (``expires_at IS NULL``) and holds until
    the holder explicitly unlocks (or an admin force-unlocks). Pass
    ``ttl_minutes`` on the request — or set RESOURCE_LOCK_TTL_MINUTES — to opt
    into a timed lock instead.
    """
    expires_at = _resolve_expiry(getattr(lock, "ttl_minutes", None))

    reason = getattr(lock, "reason", None)
    if reason is not None:
        reason = reason.strip() or None

    db_lock = models.ResourceLock(
        target_type=lock.target_type,
        target_id=lock.target_id,
        user_id=user.id,
        tenant_id=user.tenant_id,
        expires_at=expires_at,
        reason=reason,
    )

    try:
        db.add(db_lock)
        db.commit()
        db.refresh(db_lock)
        return db_lock
    except IntegrityError:
        db.rollback()
        # Re-raise with a more specific custom exception or message if you prefer
        raise


def renew_lock(
    db: Session,
    target_type: str,
    target_id: int,
    user_id: int,
    ttl_minutes: Optional[int] = None,
    reason: Optional[str] = None,
) -> Optional[models.ResourceLock]:
    """Extend (push out) the expiry of a lock held by ``user_id``.

    Returns the refreshed lock, or None when the caller does not currently hold
    an ACTIVE lock on the resource (expired locks cannot be silently renewed —
    the caller must re-acquire, which also re-checks for a competing holder).
    Optionally updates the stored reason note.
    """
    db_lock = (
        db.query(models.ResourceLock)
        .filter(
            models.ResourceLock.target_type == target_type,
            models.ResourceLock.target_id == target_id,
            models.ResourceLock.user_id == user_id,
            _active_expiry_clause(),
        )
        .first()
    )
    if not db_lock:
        return None

    ttl = _resolve_ttl_minutes(ttl_minutes)
    now = datetime.utcnow()
    db_lock.expires_at = now + timedelta(minutes=ttl)
    db_lock.renewed_at = now
    if reason is not None:
        db_lock.reason = reason.strip() or None

    db.add(db_lock)
    db.commit()
    db.refresh(db_lock)
    return db_lock


def release_lock(
    db: Session,
    target_type: str,
    target_id: int,
    user_id: int
) -> bool:
    """Release a lock on a resource held by a specific user."""
    db_lock = (
        db.query(models.ResourceLock)
        .filter(
            models.ResourceLock.target_type == target_type,
            models.ResourceLock.target_id == target_id,
            models.ResourceLock.user_id == user_id
        )
        .first()
    )

    if db_lock:
        # ISO-exception: transient coordination/session row; hard-delete required
        # (a ResourceLock is an operational mutex — releasing it must free the
        # row and its unique key so the resource can be re-locked; a tombstone
        # would never free / would collide on re-acquire).
        db.delete(db_lock)
        db.commit()
        return True
    return False


def force_release_lock(
    db: Session,
    target_type: str,
    target_id: int,
) -> Optional[dict]:
    """Administratively release a lock regardless of who holds it.

    Returns a snapshot dict of the released lock (for audit metadata), or None
    when there was no active lock to release. Caller is responsible for the
    authorization check and the audit log entry — this only performs the
    release. Locks are operational rows (no soft-delete / audit trigger on the
    table), so a hard delete here is the intended release semantics.
    """
    db_lock = get_active_lock_for_resource(db, target_type=target_type, target_id=target_id)
    if not db_lock:
        return None

    snapshot = {
        "lock_id": db_lock.id,
        "target_type": db_lock.target_type,
        "target_id": db_lock.target_id,
        "holder_user_id": db_lock.user_id,
        "tenant_id": db_lock.tenant_id,
        "reason": db_lock.reason,
        "expires_at": db_lock.expires_at.isoformat() if db_lock.expires_at else None,
    }
    # ISO-exception: transient coordination/session row; hard-delete required
    # (administrative release of an operational mutex — the lock row and its
    # unique key must be freed so the resource can be re-locked; a tombstone
    # would never free / would collide on re-acquire).
    db.delete(db_lock)
    db.commit()
    return snapshot
