"""FX.114 (owner Q101=A) — service accounts + API key lifecycle.

Tenant-owned, non-human principals (``models.ServiceAccount``) that own
API keys. Each service account carries a REQUIRED named human owner
(``owner_user_id``) — the accountable person who answers for the
credentials in access reviews. Disabling/offboarding that owner flags
every service account they own with ``owner_reassignment_required``
(notice via the existing admin email path; never a silent orphan).

API key lifetime:
- ``auth.api_key_max_lifetime_days`` (platform setting, default 365):
  enforced at create AND rotate (expires_at required and <= max).
- daily job: 14/3-day expiry reminders (deduped per key by
  reminder_14d_sent / reminder_3d_sent flags) + auto-disable of keys
  unused for ``auth.api_key_auto_disable_unused_days`` (default 90) —
  disabled, audited, never deleted.
- rotation: successor key with the same scope/owner; the old key stays
  valid until ``auth.api_key_rotation_overlap_hours`` (default 24) then
  the job auto-disables it. The new secret is returned exactly once and
  never logged.

Admin surface (tenant admin): list EVERY key in the tenant (owner user
or service account, scope, read-only, last used, expiry, disabled/
revoked state) + one-click audited revoke. Revoke = soft-delete
(ISO 27001 — never hard delete).

Access reviews: service accounts + API keys are snapshotted into GC10
tenant campaigns as items with subject_role='service-account' (one item
per service account; its live keys are carried in the item metadata and
revoking the item revokes those keys).
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

import audit
import models
from services._base import NotFoundError, ServiceError

logger = logging.getLogger(__name__)

MAX_LIFETIME_KEY = "auth.api_key_max_lifetime_days"
AUTO_DISABLE_UNUSED_KEY = "auth.api_key_auto_disable_unused_days"
OVERLAP_HOURS_KEY = "auth.api_key_rotation_overlap_hours"

SUBJECT_ROLE = "service-account"

# Admin notice recipients: active tenant admins of the tenant; for
# platform-admin actors we also include the platform admins.
_TENANT_ADMIN_EMAILS_BATCH = 500


class ForbiddenError(ServiceError):
    def __init__(self, message: str):
        super().__init__(message, code="forbidden")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    a = _aware(dt)
    return a.isoformat() if a is not None else None


def _require_tenant_admin(current_user: models.User) -> None:
    """Tenant admin = a parent user of the tenant, or a platform admin
    (same rule as the access-review campaigns service)."""
    if (current_user.parent_user_id is not None
            and not current_user.is_platform_admin):
        raise ForbiddenError("Tenant admin (parent user) only")


# --------------------------------------------------------------------------- #
# Platform-setting readers
# --------------------------------------------------------------------------- #

def max_lifetime_days(db) -> int:
    """Tenant max key lifetime in days (default 365; gov compliance
    profiles lock it to 90 through the normal profile-requirement map)."""
    from services import platform_settings as psr
    return int(psr.get_setting(MAX_LIFETIME_KEY, db=db) or 365)


def auto_disable_unused_days(db) -> int:
    """Days unused before a live key is auto-disabled (0 = off,
    default 90)."""
    from services import platform_settings as psr
    raw = psr.get_setting(AUTO_DISABLE_UNUSED_KEY, db=db)
    return 90 if raw is None else int(raw)


def rotation_overlap_hours(db) -> float:
    """Rotation overlap window in hours (default 24)."""
    from services import platform_settings as psr
    return float(psr.get_setting(OVERLAP_HOURS_KEY, db=db) or 24)


# FX.56 p1c: the old per-tenant lifetime enforcer is GONE — create/rotate
# resolve every expiry through services/credential_lifetime.py, which now
# folds auth.api_key_max_lifetime_days (and auth.api_key_max_ttl_days) into
# its effective ceiling via effective_max_lifetime_days(db, tenant_id).


# --------------------------------------------------------------------------- #
# Key material
# --------------------------------------------------------------------------- #

def generate_key_value() -> str:
    """A fresh raw key (returned to the caller exactly once, never
    logged). Hashed via security.hash_api_key by callers."""
    return f"sk_{secrets.token_urlsafe(32)}"


def _resolve_credential_expiry(db: Session, tenant_id: Optional[int],
                               expires_at: Optional[datetime],
                               now: Optional[datetime] = None
                               ) -> datetime:
    """GSEC FX.56 p1b (Q39=A) — resolve the FINITE key lifetime through
    the single credential-lifetime resolver (default 90 days, max 365,
    gov profiles capped at 90). The resolver's 422 HTTPException is
    mapped through the router's existing ServiceError path
    (unprocessable_entity -> HTTP 422).

    1788f — CE carve: services.credential_lifetime is a paid module, so CE has no
    extra lifetime ceiling to enforce. CE default = the caller's own TTL: an
    explicit ``expires_at`` is used as given (timezone-aware), otherwise this
    module's own default lifetime setting (``auth.api_key_max_lifetime_days``,
    default 365). Same return shape: a timezone-aware ``datetime``."""
    from utils.paid_import import optional_paid
    cred_lifetime = optional_paid("services.credential_lifetime")
    if cred_lifetime is None:
        moment = now or datetime.now(timezone.utc)
        if expires_at is None:
            return moment + timedelta(days=max_lifetime_days(db))
        return expires_at if expires_at.tzinfo is not None \
            else expires_at.replace(tzinfo=timezone.utc)
    try:
        return cred_lifetime.resolve_expiry_from_datetime(
            db, tenant_id, expires_at, now=now)
    except HTTPException as exc:
        detail = exc.detail
        message = detail.get("message") if isinstance(detail, dict) \
            else str(detail)
        raise ServiceError(message or "Credential lifetime rejected.",
                           code="unprocessable_entity")


def _mint_key_row(db: Session, *, user_id: int, tenant_id: int,
                  description: Optional[str], expires_at: datetime,
                  capability_scope: Optional[List[str]],
                  is_read_only: bool,
                  service_account_id: Optional[int]) -> models.ApiKey:
    """Insert the ApiKey row (no Keycloak client — service-account
    keys and admin-minted tenant keys reuse the same exchange path
    only when a KC client exists; FX.114 keys are hashed-credential
    keys, and the token-exchange endpoint's existing KC guard still
    applies to user-created keys). Caller commits."""
    from security import hash_api_key
    value = generate_key_value()
    key_prefix = value[:8]
    row = models.ApiKey(
        user_id=user_id,
        key_prefix=key_prefix,
        hashed_key=hash_api_key(value),
        description=description,
        expires_at=_aware(expires_at),
        capability_scope=capability_scope,
        is_read_only=bool(is_read_only),
        service_account_id=service_account_id,
    )
    db.add(row)
    db.flush()
    # Keep the raw value OUT of every log line: the caller's return
    # dict is the ONLY place it appears.
    return row, value


def _key_payload(row: models.ApiKey,
                 sa_by_id: Dict[int, models.ServiceAccount],
                 user_by_id: Dict[int, models.User]) -> Dict[str, Any]:
    owner = user_by_id.get(row.user_id)
    sa = sa_by_id.get(row.service_account_id) if row.service_account_id else None
    return {
        "id": row.id,
        "key_prefix": row.key_prefix,
        "description": row.description,
        "service_account_id": row.service_account_id,
        "service_account_name": sa.name if sa else None,
        "owner_user_id": sa.owner_user_id if sa else row.user_id,
        "owner_username": (owner.username if owner else None) if not sa
        else (user_by_id.get(sa.owner_user_id).username
              if user_by_id.get(sa.owner_user_id) else None),
        "is_read_only": bool(row.is_read_only),
        "capability_scope": row.capability_scope or [],
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "last_used_at": _iso(row.last_used_at),
        "disabled_at": _iso(row.disabled_at),
        "revoked_at": _iso(row.deleted_at),
        "status": ("revoked" if row.deleted_at
                   else "disabled" if row.disabled_at
                   else "active"),
        # Q87 (owner decision) — tenant web-UI/API IP allowlist does not
        # exist in the platform yet (only SCIM tokens carry one); the
        # admin list surfaces the per-key state so the column is ready.
        "ip_allowlist_status": "not_configured",
    }


# --------------------------------------------------------------------------- #
# Service-account CRUD (tenant admin)
# --------------------------------------------------------------------------- #

def _get_service_account(db: Session, sa_id: int,
                         current_user: models.User
                         ) -> models.ServiceAccount:
    sa = (
        db.query(models.ServiceAccount)
        .filter(models.ServiceAccount.id == sa_id)
        .filter(models.ServiceAccount.deleted_at.is_(None))
        .first()
    )
    # Cross-tenant access is a 404 (no existence leak).
    if (sa is None or sa.tenant_id != current_user.tenant_id
            or (current_user.is_platform_admin and False)):
        raise NotFoundError("Service account not found")
    return sa


def _owner_active(db: Session, owner_user_id: int) -> bool:
    u = db.get(models.User, owner_user_id)
    if u is None or u.deleted_at is not None:
        return False
    return u.status == models.UserStatus.ACTIVE


def create_service_account(db: Session, current_user: models.User, *,
                           name: str,
                           description: Optional[str] = None,
                           owner_user_id: int,
                           request: Optional[Request] = None,
                           ) -> Dict[str, Any]:
    """Tenant admin creates a service account. The owner MUST be an
    active user of the same tenant (REQUIRED named human owner)."""
    _require_tenant_admin(current_user)
    name = (name or "").strip()
    if not name or len(name) > 128:
        raise ServiceError("name is required (1..128 chars)",
                           code="unprocessable_entity")
    if owner_user_id is None:
        raise ServiceError(
            "owner_user_id is required: every service account must have "
            "a named human owner.",
            code="unprocessable_entity")
    owner = db.get(models.User, owner_user_id)
    if (owner is None or owner.deleted_at is not None
            or owner.tenant_id != current_user.tenant_id):
        raise ServiceError(
            "owner_user_id must be an active member of this tenant",
            code="unprocessable_entity")
    if owner.status != models.UserStatus.ACTIVE:
        raise ServiceError(
            "the owner must be an ACTIVE user",
            code="unprocessable_entity")
    dup = (
        db.query(models.ServiceAccount)
        .filter(models.ServiceAccount.tenant_id == current_user.tenant_id,
                models.ServiceAccount.name == name,
                models.ServiceAccount.deleted_at.is_(None))
        .first()
    )
    if dup is not None:
        raise ServiceError(
            f"A service account named {name!r} already exists in this "
            "tenant", code="conflict")
    sa = models.ServiceAccount(
        tenant_id=current_user.tenant_id, name=name,
        description=description, owner_user_id=owner_user_id,
    )
    db.add(sa)
    db.flush()
    audit.log_admin_action(
        db, action=models.AuditAction.SERVICE_ACCOUNT_CREATED,
        admin_user_id=current_user.id, request=request,
        target_tenant_id=sa.tenant_id,
        resource_type="service_account", resource_id=sa.id,
        metadata={"event": "service_account_created",
                  "tenant_id": sa.tenant_id, "name": name,
                  "owner_user_id": owner_user_id})
    db.commit()
    db.refresh(sa)
    return _sa_payload(db, sa)


def list_service_accounts(db: Session, current_user: models.User, *,
                          skip: int = 0, limit: int = 50,
                          ) -> Dict[str, Any]:
    _require_tenant_admin(current_user)
    q = (
        db.query(models.ServiceAccount)
        .filter(models.ServiceAccount.tenant_id == current_user.tenant_id)
        .filter(models.ServiceAccount.deleted_at.is_(None))
    )
    total = q.count()
    rows = (q.order_by(models.ServiceAccount.id)
            .offset(skip).limit(limit).all())
    return {"total": total, "items": [_sa_payload(db, sa) for sa in rows]}


def _sa_payload(db: Session, sa: models.ServiceAccount) -> Dict[str, Any]:
    owner = db.get(models.User, sa.owner_user_id)
    live_keys = (
        db.query(models.ApiKey)
        .filter(models.ApiKey.service_account_id == sa.id,
                models.ApiKey.deleted_at.is_(None))
        .all()
    )
    return {
        "id": sa.id,
        "name": sa.name,
        "description": sa.description,
        "owner_user_id": sa.owner_user_id,
        "owner_username": owner.username if owner else None,
        "owner_active": (owner is not None
                         and owner.status == models.UserStatus.ACTIVE),
        "owner_reassignment_required": bool(sa.owner_reassignment_required),
        "disabled_at": _iso(sa.disabled_at),
        "created_at": _iso(sa.created_at),
        "live_key_count": len(live_keys),
    }


def update_service_account(db: Session, current_user: models.User,
                           sa_id: int, *,
                           description: Optional[str] = None,
                           owner_user_id: Optional[int] = None,
                           request: Optional[Request] = None,
                           ) -> Dict[str, Any]:
    """Tenant admin updates a service account. Reassigning the owner to
    an active user CLEARS owner_reassignment_required."""
    _require_tenant_admin(current_user)
    sa = _get_service_account(db, sa_id, current_user)
    if owner_user_id is not None:
        owner = db.get(models.User, owner_user_id)
        if (owner is None or owner.deleted_at is not None
                or owner.tenant_id != current_user.tenant_id
                or owner.status != models.UserStatus.ACTIVE):
            raise ServiceError(
                "owner_user_id must be an active user of this tenant",
                code="unprocessable_entity")
        old_owner_user_id = sa.owner_user_id
        sa.owner_user_id = owner_user_id
        if sa.owner_reassignment_required:
            sa.owner_reassignment_required = False
            audit.log_admin_action(
                db, action=models.AuditAction.SERVICE_ACCOUNT_REASSIGNED,
                admin_user_id=current_user.id, request=request,
                target_tenant_id=sa.tenant_id,
                resource_type="service_account", resource_id=sa.id,
                metadata={"event": "service_account_reassigned",
                          "old_owner_user_id": old_owner_user_id,
                          "new_owner_user_id": owner_user_id})
    if description is not None:
        sa.description = description
    db.add(sa)
    audit.log_admin_action(
        db, action=models.AuditAction.SERVICE_ACCOUNT_UPDATED,
        admin_user_id=current_user.id, request=request,
        target_tenant_id=sa.tenant_id,
        resource_type="service_account", resource_id=sa.id,
        metadata={"event": "service_account_updated",
                  "owner_user_id": sa.owner_user_id,
                  "description": sa.description})
    db.commit()
    db.refresh(sa)
    return _sa_payload(db, sa)


def disable_service_account(db: Session, current_user: models.User,
                            sa_id: int, *, request: Optional[Request] = None,
                            ) -> Dict[str, Any]:
    """Tenant admin disables a service account. Its LIVE keys are
    disabled too (disabled_at, never deleted). Idempotent."""
    _require_tenant_admin(current_user)
    sa = _get_service_account(db, sa_id, current_user)
    now = _utcnow()
    if sa.disabled_at is None:
        sa.disabled_at = now
    for key in (db.query(models.ApiKey)
                .filter(models.ApiKey.service_account_id == sa.id,
                        models.ApiKey.deleted_at.is_(None),
                        models.ApiKey.disabled_at.is_(None))
                .all()):
        key.disabled_at = now
    audit.log_admin_action(
        db, action=models.AuditAction.SERVICE_ACCOUNT_DISABLED,
        admin_user_id=current_user.id, request=request,
        target_tenant_id=sa.tenant_id,
        resource_type="service_account", resource_id=sa.id,
        metadata={"event": "service_account_disabled"})
    db.commit()
    db.refresh(sa)
    return _sa_payload(db, sa)


# --------------------------------------------------------------------------- #
# Service-account API keys (tenant admin, audited)
# --------------------------------------------------------------------------- #

def create_service_account_key(db: Session, current_user: models.User,
                               sa_id: int, *,
                               description: Optional[str] = None,
                               expires_at: Optional[datetime] = None,
                               capability_scope: Optional[List[str]] = None,
                               is_read_only: bool = False,
                               request: Optional[Request] = None,
                               ) -> Dict[str, Any]:
    """Tenant admin mints a key owned by the service account.

    The NAMED HUMAN OWNER of the service account is the key's user_id
    (the accountable principal the auth funnel resolves); the key is
    scoped to the service account via service_account_id. Lifetime is
    enforced (expires_at required, <= max). The raw secret is returned
    ONCE and is never logged."""
    _require_tenant_admin(current_user)
    sa = _get_service_account(db, sa_id, current_user)
    if sa.disabled_at is not None:
        raise ServiceError(
            "Service account is disabled; re-enable before minting keys.",
            code="conflict")
    if not _owner_active(db, sa.owner_user_id):
        raise ServiceError(
            "The service account's owner is not active; reassign an "
            "active owner first.", code="conflict")
    resolved_expires = _resolve_credential_expiry(
        db, current_user.tenant_id, expires_at)
    row, value = _mint_key_row(
        db, user_id=sa.owner_user_id,
        tenant_id=current_user.tenant_id,
        description=description, expires_at=resolved_expires,
        capability_scope=capability_scope, is_read_only=is_read_only,
        service_account_id=sa.id)
    audit.log_admin_action(
        db, action=models.AuditAction.AUTH_API_KEY_CREATED,
        admin_user_id=current_user.id, request=request,
        target_tenant_id=current_user.tenant_id,
        resource_type="api_key", resource_id=row.id,
        metadata={"event": "service_account_key_created",
                  "service_account_id": sa.id,
                  "key_prefix": row.key_prefix,
                  "expires_at": _iso(row.expires_at)})
    db.commit()
    # The secret appears ONLY in this dict — never in logs/audit rows.
    return {**_key_payload(row, {sa.id: sa},
                           {row.user_id: db.get(models.User, row.user_id)}),
            "access_key": value}


def rotate_service_account_key(db: Session, current_user: models.User,
                               key_id: int, *,
                               request: Optional[Request] = None,
                               ) -> Dict[str, Any]:
    """FX.114 rotation: mint a SUCCESSOR key with the same scope/owner
    and start the old key's overlap window (auth.api_key_
    rotation_overlap_hours, default 24h). The job auto-disables the
    old key when the window ends. The new secret is returned exactly
    once, never logged."""
    _require_tenant_admin(current_user)
    old = (db.query(models.ApiKey)
           .filter(models.ApiKey.id == key_id,
                   models.ApiKey.deleted_at.is_(None))
           .first())
    if old is None:
        raise NotFoundError("API key not found")
    if old.service_account_id is None:
        # Rotation is a service-account-key feature; user keys use the
        # existing user flow (create new, revoke old).
        raise ServiceError(
            "Only service-account keys support in-place rotation; "
            "create a new key and revoke this one instead.",
            code="unprocessable_entity")
    sa = db.get(models.ServiceAccount, old.service_account_id)
    if sa is None or sa.deleted_at is not None:
        raise NotFoundError("Service account not found")
    if sa.tenant_id != current_user.tenant_id:
        raise NotFoundError("API key not found")
    if old.disabled_at is not None or sa.disabled_at is not None:
        raise ServiceError(
            "Key/service account is disabled; rotate only live keys.",
            code="conflict")
    now = _utcnow()
    overlap = timedelta(hours=rotation_overlap_hours(db))
    new_expires = _resolve_credential_expiry(db, current_user.tenant_id,
                                             old.expires_at, now=now)
    # A successor that would inherit an EXPIRED window is refused (the
    # old key is already dead; nothing to rotate).
    if _aware(new_expires) is not None and _aware(new_expires) <= now:
        raise ServiceError(
            "The key has already expired; create a fresh key instead.",
            code="conflict")
    row, value = _mint_key_row(
        db, user_id=old.user_id,
        tenant_id=current_user.tenant_id,
        description=old.description, expires_at=_aware(new_expires),
        capability_scope=old.capability_scope,
        is_read_only=bool(old.is_read_only),
        service_account_id=sa.id)
    row.rotates_to_key_id = None
    old.rotates_to_key_id = row.id
    old.rotation_disable_at = now + overlap
    audit.log_admin_action(
        db, action=models.AuditAction.API_KEY_ROTATED,
        admin_user_id=current_user.id, request=request,
        target_tenant_id=sa.tenant_id,
        resource_type="api_key", resource_id=row.id,
        metadata={"event": "api_key_rotated",
                  "old_key_id": old.id, "new_key_id": row.id,
                  "service_account_id": sa.id,
                  "rotation_disable_at": _iso(old.rotation_disable_at)})
    db.commit()
    return {**_key_payload(row, {sa.id: sa},
                           {row.user_id: db.get(models.User, row.user_id)}),
            "access_key": value,
            "old_key_id": old.id,
            "old_key_rotation_disable_at": _iso(old.rotation_disable_at)}


# --------------------------------------------------------------------------- #
# Admin keys list + one-click revoke (tenant admin)
# --------------------------------------------------------------------------- #

def admin_list_keys(db: Session, current_user: models.User, *,
                    skip: int = 0, limit: int = 50,
                    ) -> Dict[str, Any]:
    """Tenant admin: EVERY key in the tenant (owner user or service
    account, scope, read-only, last used, expiry, disabled/revoked
    state). Q87's IP allowlist does not exist for web/API keys yet
    (only SCIM tokens), so ip_allowlist_status is surfaced as
    'not_configured' — the column is ready for Q87's builder."""
    _require_tenant_admin(current_user)
    tenant_id = current_user.tenant_id
    rows = (
        db.query(models.ApiKey, models.User)
        .outerjoin(models.User, models.User.id == models.ApiKey.user_id)
        .filter(models.ApiKey.deleted_at.is_(None))
        .filter(models.User.tenant_id == tenant_id)
        .order_by(models.ApiKey.id)
        .offset(skip).limit(limit)
        .all()
    )
    total = (
        db.query(models.ApiKey.id)
        .outerjoin(models.User, models.User.id == models.ApiKey.user_id)
        .filter(models.ApiKey.deleted_at.is_(None))
        .filter(models.User.tenant_id == tenant_id)
        .count()
    )
    sa_ids = {r.service_account_id for r, _ in rows
              if r.service_account_id}
    sa_by_id = {sa.id: sa for sa in db.query(models.ServiceAccount)
                .filter(models.ServiceAccount.id.in_(sa_ids)).all()} \
        if sa_ids else {}
    owner_ids = {r.user_id for r, _ in rows if r.user_id}
    for sa in sa_by_id.values():
        owner_ids.add(sa.owner_user_id)
    user_by_id = {u.id: u for u in db.query(models.User)
                  .filter(models.User.id.in_(owner_ids)).all()} \
        if owner_ids else {}
    return {"total": total,
            "items": [_key_payload(r, sa_by_id, user_by_id)
                      for r, _ in rows]}


def admin_revoke_key(db: Session, current_user: models.User,
                     key_id: int, *,
                     request: Optional[Request] = None,
                     ) -> Dict[str, Any]:
    """Tenant admin one-click revoke: soft-delete (ISO — never hard
    delete) + KC client cleanup when present + audit. The actor (not
    the owner) is attributed, so a tenant admin can revoke any key in
    the tenant."""
    _require_tenant_admin(current_user)
    key = (db.query(models.ApiKey)
           .filter(models.ApiKey.id == key_id,
                   models.ApiKey.deleted_at.is_(None))
           .first())
    if key is None:
        raise NotFoundError("API key not found")
    owner = db.get(models.User, key.user_id)
    if owner is None or owner.tenant_id != current_user.tenant_id:
        raise NotFoundError("API key not found")
    if key.keycloak_client_uuid:
        try:
            from keycloak_auth import keycloak_admin_client
            keycloak_admin_client.delete_service_account_client(
                key.keycloak_client_uuid)
        except Exception as e:  # noqa: BLE001 — KC down: revoke anyway
            logger.warning(
                "admin_revoke_key: KC client delete failed "
                "(key=%s): %s", key.id, e)
    now = _utcnow()
    key.deleted_at = now
    audit.log_admin_action(
        db, action=models.AuditAction.API_KEY_ADMIN_REVOKED,
        admin_user_id=current_user.id, request=request,
        target_tenant_id=current_user.tenant_id,
        resource_type="api_key", resource_id=key.id,
        metadata={"event": "api_key_admin_revoked",
                  "owner_user_id": key.user_id,
                  "service_account_id": key.service_account_id,
                  "key_prefix": key.key_prefix})
    db.commit()
    return {"id": key.id, "revoked_at": _iso(now), "status": "revoked"}


# --------------------------------------------------------------------------- #
# User-disable hook: flag service accounts owned by the offboarded user
# --------------------------------------------------------------------------- #

def flag_service_accounts_for_user(db: Session, user_id: int, *,
                                   actor_user_id: Optional[int] = None,
                                   request: Optional[Request] = None,
                                   ) -> List[int]:
    """FX.114 — when a user is disabled/offboarded, EVERY service
    account they own is flagged ``owner_reassignment_required`` and
    the tenant admins are notified via the existing email path (best
    effort — a send failure never blocks the deactivation). Idempotent.

    Returns the list of flagged service-account ids."""
    flagged = (
        db.query(models.ServiceAccount)
        .filter(models.ServiceAccount.owner_user_id == user_id,
                models.ServiceAccount.deleted_at.is_(None),
                models.ServiceAccount.owner_reassignment_required.is_(False))
        .all()
    )
    if not flagged:
        return []
    ids = []
    for sa in flagged:
        sa.owner_reassignment_required = True
        db.add(sa)
        ids.append(sa.id)
    tenant_id = flagged[0].tenant_id
    try:
        audit.log_admin_action(
            db, action=models.AuditAction.SERVICE_ACCOUNT_UPDATED,
            admin_user_id=actor_user_id or user_id, request=request,
            target_tenant_id=tenant_id,
            resource_type="service_account", resource_id=ids[0],
            metadata={"event": "service_accounts_owner_reassignment_required",
                      "service_account_ids": ids, "owner_user_id": user_id,
                      "tenant_id": tenant_id})
    except Exception as e:  # noqa: BLE001 — audit must not block deactivation
        logger.warning("service_accounts: owner-flag audit failed: %s", e)
    _notify_admins_owner_flag(db, tenant_id, ids, user_id)
    db.commit()
    return ids


def _tenant_admin_emails(db: Session, tenant_id: int) -> List[str]:
    rows = (db.query(models.User.email)
            .filter(models.User.tenant_id == tenant_id,
                    models.User.status == models.UserStatus.ACTIVE)
            .all())
    return [e for (e,) in rows if e]


def _notify_admins_owner_flag(db: Session, tenant_id: int,
                              sa_ids: List[int], user_id: int) -> None:
    """In-app + email notice to tenant admins that reassignment is
    required (existing email service; best effort)."""
    try:
        from services import email_send_api
        to = _tenant_admin_emails(db, tenant_id)
        if not to:
            return
        names = ", ".join(
            str(n) for (n,) in db.query(models.ServiceAccount.name)
            .filter(models.ServiceAccount.id.in_(sa_ids)).all())
        email_send_api.prepare_send(
            db, tenant_id=tenant_id, user_id=None, app_id=None,
            to=to, cc=[], bcc=[],
            subject=("Service account owner reassignment required"),
            text=(
                f"The owner of service account(s) {names} was disabled "
                f"or offboarded. Assign a new active owner to keep the "
                f"credentials accountable."
            ),
            html=None, attachments=None,
        )
    except Exception as e:  # noqa: BLE001 — notice failure is non-fatal
        logger.warning("service_accounts: owner-flag notice failed: %s", e)


# --------------------------------------------------------------------------- #
# Daily lifecycle job: expiry reminders + unused auto-disable + rotation
# --------------------------------------------------------------------------- #

def _send_key_reminder(db: Session, key: models.ApiKey,
                       which: str) -> bool:
    """Email the key owner + tenant admins about an upcoming expiry.
    True on send success (the dedupe flag is set ONLY by the caller
    after this returns True, so a failed send retries next tick)."""
    owner = db.get(models.User, key.user_id)
    to: List[str] = []
    if owner is not None and owner.email:
        to.append(owner.email)
    if owner is not None:
        to.extend(_tenant_admin_emails(db, owner.tenant_id))
    if not to:
        return False
    try:
        from services import email_send_api
        email_send_api.prepare_send(
            db, tenant_id=owner.tenant_id if owner else 1, user_id=None,
            app_id=None, to=sorted(set(to)), cc=[], bcc=[],
            subject=(f"API key {key.key_prefix}... expires soon "
                     f"({which}-day reminder)"),
            text=(
                f"API key {key.key_prefix}... (description: "
                f"{key.description or '-'}) expires at "
                f"{_iso(key.expires_at)} (UTC). Rotate it before "
                f"expiry to avoid an outage."
            ),
            html=None, attachments=None,
        )
        return True
    except Exception as e:  # noqa: BLE001 — sweep continues
        logger.warning("service_accounts: key reminder %s failed for "
                       "key %s: %s", which, key.id, e)
        return False


def _audit_auto_disable(db: Session, key: models.ApiKey, reason: str,
                        tenant_id: Optional[int]) -> None:
    """System-job audit (actor_user_id=0 = the scheduler, same pattern
    as scim_reconcile). Audit write failures never block the sweep."""
    try:
        audit.log_permission_audit(
            db=db,
            action=models.AuditAction.API_KEY_AUTO_DISABLED,
            actor_user_id=0,
            tenant_id=tenant_id or 1,
            resource_type="api_key", resource_id=key.id,
            target_user_id=key.user_id,
            metadata={"event": "api_key_auto_disabled",
                      "reason": reason,
                      "key_prefix": key.key_prefix,
                      "service_account_id": key.service_account_id})
    except Exception as e:  # noqa: BLE001 — audit must not block the sweep
        logger.warning("service_accounts: auto-disable audit failed "
                       "(key=%s): %s", key.id, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def lifecycle_tick(db: Session) -> Dict[str, Any]:
    """FX.114 daily job (scheduler, id fx114_api_key_lifecycle):

    1. EXPIRY REMINDERS — live keys with expires_at within 14 days
       (flag reminder_14d_sent) and within 3 days (flag
       reminder_3d_sent). Each horizon fires ONCE per key: the flag is
       set only after a successful send, a failed send retries next
       tick (same dedupe contract as services.dsar_tracker_service).
    2. UNUSED AUTO-DISABLE — live, not-yet-disabled keys whose
       last_used_at (or created_at, if never used) is older than
       auth.api_key_auto_disable_unused_days (default 90) get
       disabled_at stamped + audited. NEVER deleted.
    3. ROTATION OVERLAP — old keys whose rotation_disable_at has passed
       get disabled_at stamped + audited.

    Returns counters for the scheduler log line."""
    now = _utcnow()
    d14 = now + timedelta(days=14)
    d3 = now + timedelta(days=3)
    unused_days = auto_disable_unused_days(db)
    counters = {"checked": 0, "reminded_14d": 0, "reminded_3d": 0,
                "auto_disabled_unused": 0, "auto_disabled_rotation": 0}
    live = (db.query(models.ApiKey)
            .filter(models.ApiKey.deleted_at.is_(None),
                    models.ApiKey.disabled_at.is_(None))
            .all())
    for key in live:
        counters["checked"] += 1
        try:
            exp = _aware(key.expires_at)
            if exp is not None:
                if not key.reminder_14d_sent and exp <= d14:
                    if _send_key_reminder(db, key, "14"):
                        key.reminder_14d_sent = True
                        db.add(key)
                        db.commit()
                        counters["reminded_14d"] += 1
                if not key.reminder_3d_sent and exp <= d3:
                    if _send_key_reminder(db, key, "3"):
                        key.reminder_3d_sent = True
                        db.add(key)
                        db.commit()
                        counters["reminded_3d"] += 1

            rot_due = _aware(key.rotation_disable_at)
            if rot_due is not None and rot_due <= now:
                key.disabled_at = now
                db.add(key)
                owner = db.get(models.User, key.user_id)
                _audit_auto_disable(db, key, "rotation_overlap_expired",
                                    owner.tenant_id if owner else None)
                db.commit()
                counters["auto_disabled_rotation"] += 1
                continue

            if unused_days > 0:
                anchor = _aware(key.last_used_at) or _aware(
                    key.created_at) or now
                if now - anchor > timedelta(days=unused_days):
                    key.disabled_at = now
                    db.add(key)
                    owner = db.get(models.User, key.user_id)
                    _audit_auto_disable(db, key, "unused",
                                        owner.tenant_id if owner else None)
                    db.commit()
                    counters["auto_disabled_unused"] += 1
        except Exception as e:  # noqa: BLE001 — one bad row never stops
            logger.warning("service_accounts: lifecycle tick failed for "
                           "key %s: %s", key.id, e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return counters
