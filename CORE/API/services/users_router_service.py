"""users-router service — business logic for `routers/users.py`
(Gate 2b migration).

Data access:
    * ordinary tenant-scoped / non-deleted user & tenant CRUD reuses the
      EXISTING `crud/user_cud.py` + `crud/tenant_cud.py` functions, imported
      via the top-level `crud` package (`import crud`) — several are already
      patched by name in `tests/test_users_router.py`
      (`crud.get_user_in_tenant`, `crud.get_users_in_tenant`,
      `crud.update_user`, `crud.deactivate_user`, `crud.reactivate_user`,
      `crud.delete_user_permanently`, `crud.get_child_users`,
      `crud.create_tenant_with_default_roles`); calling them through the
      `crud` module attribute (not a bound `from crud import x`) keeps those
      monkeypatches effective.
    * the admin-only / include-deleted / unbounded lookups this router also
      needs (that `user_cud`/`tenant_cud` don't already provide) live in the
      new `crud/users_router.py`, imported here as `users_crud`.

404-vs-403 (shared rule #5): every fetch below is either tenant-scoped at the
SQL level (`crud.get_user_in_tenant`, `crud.get_users_in_tenant`) so a
cross-tenant target is a plain "not found", or is the deliberate
platform-admin god-mode / same-tenant-admin lookup the original router used
(`users_crud.get_user_include_deleted`,
`users_crud.get_user_in_tenant_include_deleted`,
`users_crud.get_tenant_include_deleted`) — the capability/role gate that
would otherwise produce a 403 runs BEFORE the fetch (in
`resolve_target_user_for_admin`, and via the router's `require_capability` /
`is_platform_admin` checks), so a missing row after a passed gate is
correctly a 404, never a collapsed 403.

`services._base` has no `ForbiddenError` (shared rule #5) — declared here.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from sqlalchemy.orm import Session

import crud
import models
import schemas
from crud import users_router as users_crud
from services._base import ConflictError, NotFoundError, ServiceError

logger = logging.getLogger(__name__)


class ForbiddenError(ServiceError):
    """Authenticated caller lacks authorization for the action (router -> 403)."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class BadRequestError(ServiceError):
    """Domain validation failure (router -> 400) — mirrors the pre-migration
    inline `HTTPException(400, ...)` call sites verbatim."""

    def __init__(self, message: str = "Bad request", *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


# --------------------------------------------------------------------------- #
# GET /
# --------------------------------------------------------------------------- #

def list_users(
    db: Session,
    current_user: "models.User",
    *,
    tenant_id: Optional[int],
    skip: int,
    limit: int,
) -> List["models.User"]:
    """Resolve which tenant's users to list, then delegate to the existing
    tenant-scoped CRUD.

    - Regular users: always their own tenant (a non-admin passing `tenant_id`
      is rejected, matching the pre-migration inline check).
    - Platform admins: may specify any `tenant_id`.
    """
    if tenant_id is not None:
        if not current_user.is_platform_admin:
            raise ForbiddenError("Only platform admins can view users from other tenants")
        target_tenant_id = tenant_id
    else:
        target_tenant_id = current_user.tenant_id

    return crud.get_users_in_tenant(db, tenant_id=target_tenant_id, skip=skip, limit=limit)


# --------------------------------------------------------------------------- #
# GET /tenants
# --------------------------------------------------------------------------- #

def list_tenants(db: Session, current_user: "models.User") -> List["models.Tenant"]:
    """Platform-admin-only tenant listing."""
    if not current_user.is_platform_admin:
        raise ForbiddenError("Only platform admins can list all tenants")
    return users_crud.list_tenants(db)


# --------------------------------------------------------------------------- #
# DELETE /tenants/{tenant_id}
# --------------------------------------------------------------------------- #

def delete_tenant(db: Session, current_user: "models.User", tenant_id: int) -> None:
    """Soft-delete a tenant (Platform Admin only) + cascade-soft-delete its
    users + flip its Harbor project read-only + audit. Idempotent."""
    if not current_user.is_platform_admin:
        raise ForbiddenError("Only platform admins can delete tenants")

    if tenant_id == 1:
        raise BadRequestError("The platform tenant (id=1) cannot be deleted")

    if current_user.tenant_id == tenant_id:
        raise BadRequestError("You cannot delete your own tenant")

    tenant = users_crud.get_tenant_include_deleted(db, tenant_id)
    if tenant is None:
        raise NotFoundError("Tenant not found")

    if tenant.deleted_at is not None:
        return  # idempotent

    # Cascade-soft-delete every user in this tenant using the same scrubbing
    # function used by the user-delete endpoint. Don't touch Keycloak here
    # per-user — individual user KC deletion is the responsibility of the
    # invite/session flow. We just mark the tenant gone; users can't log in
    # because their tenant is deleted.
    tenant_users = users_crud.get_tenant_users_for_cascade_delete(db, tenant_id)
    for u in tenant_users:
        crud.delete_user_permanently(db=db, db_user=u)

    now = datetime.now(timezone.utc)
    tenant.name = f"Deleted Tenant {tenant.id}"
    tenant.deleted_at = now
    retention = getattr(tenant, "retention_days", 90) or 90
    tenant.hard_delete_after = now + timedelta(days=retention)
    db.add(tenant)
    db.commit()

    # Flip the tenant's Harbor project to read-only so operators can still
    # pull for forensics but nobody can push. Revokes all robot accounts;
    # Harbor admin retains access. Failure here is logged but not fatal
    # (the DB row is already marked; scheduler will retry at hard-delete
    # time). Skip when tenant Harbor is disabled (local dev).
    # GS.PSR.8 — harbor.tenant_harbor_enabled via the platform-settings
    # registry (DB > env > True).
    # Paid-only Harbor flip: skip entirely when the client is absent on CE.
    from utils.paid_import import optional_paid
    _harbor = optional_paid("utils.tenant_harbor_client")
    if _harbor is not None and _harbor.tenant_harbor_enabled(db):
        try:
            _harbor.TenantHarborClient().set_project_read_only(tenant_id)
        except Exception as e:
            logger.warning(
                "Could not flip Harbor project to read-only for tenant %s: %s",
                tenant_id, e,
            )

    # Audit (ISO 27001 §A.18.1.4 — data deletion / retention).
    try:
        from audit import log_permission_audit
        from models import AuditAction
        log_permission_audit(
            db=db, action=AuditAction.ADMIN_TENANT_DELETE,
            actor_user_id=current_user.id, tenant_id=tenant_id,
            resource_type="tenant", resource_id=tenant_id,
            metadata={"retention_days": retention,
                      "hard_delete_after": tenant.hard_delete_after.isoformat() if tenant.hard_delete_after else None},
        )
    except Exception:
        pass

    return


# --------------------------------------------------------------------------- #
# POST /tenants
# --------------------------------------------------------------------------- #

def create_tenant(
    db: Session, current_user: "models.User", tenant_data: "schemas.TenantCreate"
) -> "models.Tenant":
    """Create a new tenant organization (Platform Admin only)."""
    if not current_user.is_platform_admin:
        raise ForbiddenError("Only platform admins can create tenants")

    # GT.D7 — single-tenant guard: CE (and any non-multi-tenant tier) may only
    # hold one tenant. Raises TenantLimitError (ServiceError, code "conflict")
    # which the router maps to a 409.
    from services.tenant_limits import assert_can_create_tenant
    assert_can_create_tenant(db)

    existing_tenant = users_crud.get_tenant_by_name(db, tenant_data.name)
    if existing_tenant:
        raise ConflictError(f"A tenant with the name '{tenant_data.name}' already exists")

    new_tenant = crud.create_tenant_with_default_roles(db=db, tenant_data=tenant_data)

    # Audit (ISO 27001 §A.12.1.2 — change management).
    try:
        from models import AuditEvent
        db.add(AuditEvent(
            tenant_id=new_tenant.id, user_id=current_user.id,
            event_name="ADMIN_TENANT_CREATED",
            details={"tenant_id": new_tenant.id, "tenant_name": new_tenant.name},
        ))
        db.commit()
    except Exception:
        logger.exception("audit emission failed for tenant create")

    # GC.PFM.9 — provisioning hook: dispatch the calibration-fixture
    # migration for the new tenant (marks it pending + enqueues the
    # fixture-migrator container task). Best-effort: tenant creation must
    # never fail because of fixtures, so any error here is only logged.
    # Lead review 2026-09-29: gated on the PFM.19 cutover flag. Until the
    # migrator image is in Harbor, dispatching would leave every new tenant
    # stuck 'pending' behind the PFM.10 calibration gate.
    # FIX-FIXTURE-GATE-2 (REV-FIXTURE-GATE M2): the flag read is inside the
    # try too; a malformed value fails to the default (off) and is only logged.
    try:
        from services.platform_settings import get_setting
        if not get_setting("fixture_shared_bucket_enabled"):
            return new_tenant
    except Exception as exc:
        logger.warning(
            "fixture flag read failed for new tenant %s (%s); treating as off",
            new_tenant.id, type(exc).__name__,
        )
        return new_tenant
    try:
        from services import fixture_service
        fixture_service.dispatch_migration(db, new_tenant.id)
    except Exception:
        logger.exception(
            "fixture migration dispatch failed for new tenant %s", new_tenant.id
        )

    return new_tenant


# --------------------------------------------------------------------------- #
# GET /{user_id}
# --------------------------------------------------------------------------- #

def get_user(db: Session, current_user: "models.User", user_id: int) -> "models.User":
    """Fetch a user from the caller's own tenant, or raise `NotFoundError`."""
    db_user = crud.get_user_in_tenant(db, user_id=user_id, tenant_id=current_user.tenant_id)
    if db_user is None:
        raise NotFoundError("User not found")
    return db_user


# --------------------------------------------------------------------------- #
# PATCH /{user_id}
# --------------------------------------------------------------------------- #

def update_user(
    db: Session,
    current_user: "models.User",
    user_id: int,
    user_update: "schemas.UserUpdate",
) -> "models.User":
    """Update a user's role/status. Same-tenant only; an admin cannot change
    their own role/status via this endpoint."""
    db_user = crud.get_user_in_tenant(db, user_id=user_id, tenant_id=current_user.tenant_id)
    if db_user is None:
        raise NotFoundError("User not found")

    if db_user.id == current_user.id:
        raise ForbiddenError("Admins cannot change their own role or status via this endpoint.")

    updated = crud.update_user(db=db, db_user=db_user, user_update=user_update)

    # Audit (ISO 27001 §A.9.2.5 / SOC 2 CC6.2 — user access review).
    # ADMIN_USER_MODIFY records who changed which user, with the
    # changed-fields list captured in audit_metadata.
    try:
        from audit import log_permission_audit
        from models import AuditAction
        log_permission_audit(
            db=db, action=AuditAction.ADMIN_USER_MODIFY,
            actor_user_id=current_user.id, tenant_id=current_user.tenant_id,
            resource_type="user", resource_id=user_id,
            target_user_id=user_id,
            metadata={
                "fields_changed": [
                    k for k in user_update.model_dump(exclude_unset=True).keys()
                ],
            },
        )
    except Exception:
        pass

    return updated


# --------------------------------------------------------------------------- #
# Shared lookup + authz for the deactivate/reactivate admin actions
# --------------------------------------------------------------------------- #

def resolve_target_user_for_admin(
    db: Session, current_user: "models.User", user_id: int, capability: str
) -> Optional["models.User"]:
    """Shared lookup + authz for the deactivate/reactivate admin actions.

    - A PLATFORM ADMIN may act on a user in ANY tenant (god-mode, consistent
      with resource-lock force-unlock and role-preset apply). This is what the
      user-management tenant selector needs; the ``require_capability`` gate is
      NOT used here because it has no platform-admin bypass and would 403 a
      platform admin who holds no capability grant in their own tenant.
    - A TENANT ADMIN / any non-platform user may only act within their OWN
      tenant, and must hold the named USER capability (``deactivate`` /
      ``reactivate``) via their role/grants — a standard user without it gets
      403. A cross-tenant target for a non-admin is a plain 404 (no leak).

    Returns the target user row (include_deleted so a soft-deleted user can be
    reactivated), or None (router/caller maps to 404). Raises `ForbiddenError`
    when the non-platform caller lacks the capability.
    """
    if current_user.is_platform_admin:
        return users_crud.get_user_include_deleted(db, user_id)
    # Non-platform: enforce the USER capability, then own-tenant scope.
    from resource_permissions import authorize
    if not authorize(db, current_user, models.ResourceType.USER, capability):
        raise ForbiddenError(f"User does not have the required 'user:{capability}' capability.")
    return crud.get_user_in_tenant(db, user_id=user_id, tenant_id=current_user.tenant_id)


# --------------------------------------------------------------------------- #
# PATCH /{user_id}/deactivate
# --------------------------------------------------------------------------- #

def deactivate_user(db: Session, current_user: "models.User", user_id: int) -> "models.User":
    """Deactivate a user (soft delete + API-key revocation). Self-deactivation
    is rejected."""
    db_user = resolve_target_user_for_admin(db, current_user, user_id, "deactivate")

    if db_user is None:
        raise NotFoundError("User not found")

    if db_user.id == current_user.id:
        raise ForbiddenError("You cannot deactivate yourself.")

    deactivated = crud.deactivate_user(db=db, db_user=db_user)

    # Audit (ISO 27001 §A.9.2.6 / SOC 2 CC6.3 — deactivation). Attribute to the
    # TARGET user's tenant (may differ from the platform admin's own).
    try:
        from audit import log_permission_audit
        from models import AuditAction
        log_permission_audit(
            db=db, action=AuditAction.ADMIN_USER_DEACTIVATE,
            actor_user_id=current_user.id, tenant_id=db_user.tenant_id,
            resource_type="user", resource_id=user_id,
            target_user_id=user_id,
        )
    except Exception:
        pass

    # FX.114 (owner Q101=A) — every service account this user owns is
    # flagged owner_reassignment_required (+ tenant-admin notice).
    # Best-effort: the deactivation itself must never fail on this.
    try:
        from services import service_accounts as _sa
        _sa.flag_service_accounts_for_user(
            db, user_id, actor_user_id=current_user.id)
    except Exception as _fx114_e:  # noqa: BLE001
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "FX.114 service-account owner flag failed (user %s): %s",
            user_id, _fx114_e)

    return deactivated


# --------------------------------------------------------------------------- #
# POST /{user_id}/disable  (FX.112.p3b — one-click disable)
# --------------------------------------------------------------------------- #

def disable_user_one_click(db: Session, current_user: "models.User",
                           user_id: int) -> dict:
    """One-click disable (FX.112.p3b): in ONE call, soft-deactivate the user
    (status + API-key revocation), disable the Keycloak user (enabled=false)
    and sign them out of every active Keycloak session. Soft state only —
    nothing is hard-deleted.

    Authorization reuses the deactivate model (`resolve_target_user_for_admin`
    on the USER `deactivate` capability): a tenant admin / capable user acts
    only within their OWN tenant (a cross-tenant target is a plain 404, no
    leak); the platform-admin god-mode path is inherited as-is. Two extra
    guards on top: the actor cannot disable themselves, and the target
    cannot be the LAST active admin of their tenant (orphan lock — enforced
    even against a platform admin).

    Keycloak work is best-effort and reported in the result: if the KC
    disable / logout-all calls fail (KC outage, missing keycloak_user_id)
    the DB deactivation — the enforcement point on the API side — still
    stands; `keycloak` flags in the returned dict say what actually landed.
    """
    db_user = resolve_target_user_for_admin(db, current_user, user_id,
                                            "deactivate")
    if db_user is None:
        raise NotFoundError("User not found")

    if db_user.id == current_user.id:
        raise ForbiddenError("You cannot disable yourself.")

    # Last-admin guard: disabling `db_user` must not leave their tenant
    # without a single active admin. "Admin" = tenant parent user
    # (`parent_user_id IS None`), same predicate the MFA last-admin
    # recovery gate uses (services/mfa_recovery.py).
    active_admin_ids = [
        row[0] for row in db.query(models.User.id).filter(
            models.User.tenant_id == db_user.tenant_id,
            models.User.parent_user_id.is_(None),
            models.User.status == models.UserStatus.ACTIVE,
            models.User.deleted_at.is_(None),
        ).all()
    ]
    if db_user.id in active_admin_ids and len(active_admin_ids) == 1:
        raise ConflictError(
            "Cannot disable the last active admin of this tenant.")

    # How many live API keys this call will revoke (the deactivation below
    # soft-deletes them all).
    api_keys_revoked = (
        db.query(models.ApiKey)
        .filter(models.ApiKey.user_id == db_user.id,
                models.ApiKey.deleted_at.is_(None))
        .count()
    )

    # Soft state: status=DEACTIVATED + every live API key soft-revoked
    # (per-key AUTH_API_KEY_REVOKED audit rows are written by the revoke
    # path — ISO 27001 A.9.2.6). Reuses the existing deactivation path.
    deactivated = crud.deactivate_user(db=db, db_user=db_user)

    # Keycloak side — best-effort, never fails the call.
    kc_user_id = getattr(db_user, "keycloak_user_id", None)
    kc_disabled = False
    sessions_revoked = False
    if kc_user_id:
        try:
            from services.inactive_accounts import _disable_in_keycloak
            kc_disabled = _disable_in_keycloak(kc_user_id) == ""
        except Exception as exc:  # noqa: BLE001 — KC outage: DB state stands
            logger.warning("FX.112.p3b KC disable failed for user %s: %s",
                           user_id, exc)
        # FX.162.p5e — the CE carve strips services/account_security_alerts, so
        # the logout-all step is skipped there via the repo's paid-module loader
        # (CE degrades cleanly: sessions_revoked stays False). Only the module's
        # ABSENCE is tolerated: a bare `except Exception` here also swallowed
        # real bugs inside the present module, and the paid call itself is
        # already best-effort (revoke_keycloak_user_sessions returns False
        # instead of raising), so nothing else is caught.
        from utils.paid_import import optional_paid
        _fx112 = optional_paid("services.account_security_alerts")
        if _fx112 is not None:
            sessions_revoked = bool(
                _fx112.revoke_keycloak_user_sessions(db_user))

    # Audit — the deactivation event, with what the Keycloak side did
    # (attribute to the TARGET user's tenant, as the deactivate path does).
    try:
        from audit import log_permission_audit
        from models import AuditAction
        log_permission_audit(
            db=db, action=AuditAction.ADMIN_USER_DEACTIVATE,
            actor_user_id=current_user.id, tenant_id=db_user.tenant_id,
            resource_type="user", resource_id=user_id,
            target_user_id=user_id,
            metadata={"one_click_disable": True,
                      "keycloak_enabled_false": kc_disabled,
                      "keycloak_sessions_revoked": sessions_revoked,
                      "api_keys_revoked": api_keys_revoked},
        )
    except Exception:
        pass

    # FX.114 (owner Q101=A) — service accounts this user owns are flagged
    # owner_reassignment_required. Best-effort: same block the plain
    # deactivation uses; the disable itself must never fail on this.
    try:
        from services import service_accounts as _sa
        _sa.flag_service_accounts_for_user(
            db, user_id, actor_user_id=current_user.id)
    except Exception as _fx114_e:  # noqa: BLE001
        logger.warning(
            "FX.112.p3b service-account owner flag failed (user %s): %s",
            user_id, _fx114_e)

    return {
        "user": deactivated,
        "api_keys_revoked": api_keys_revoked,
        "keycloak": {"enabled_false": kc_disabled,
                     "sessions_revoked": sessions_revoked},
    }


# --------------------------------------------------------------------------- #
# POST /{user_id}/signout-everywhere  (FX.112.p4b — tenant-admin sign-out)
# --------------------------------------------------------------------------- #

def sign_out_user_everywhere(db: Session, current_user: "models.User",
                             user_id: int) -> dict:
    """FX.112.p4b — tenant-admin "sign out everywhere" for a user in the
    admin's OWN tenant.

    Reuses the self-service core
    (``services/me_auth_apikeys_service.sign_out_everywhere``): every
    session record of the target user is revoked + all their Keycloak
    sessions (best-effort — a KC outage never breaks the DB revocation),
    and the action is audited (SESSION_SIGN_OUT_ALL with
    ``initiator_user_id`` set to the admin). The target user's account
    state is untouched — this kills their logins only (for a full
    disable, use ``disable_user_one_click``).

    Authorization reuses the deactivate model
    (``resolve_target_user_for_admin`` with the USER `deactivate`
    capability): tenant admins act only within their own tenant (a
    cross-tenant target is a plain 404, no leak); the platform-admin
    god-mode path is inherited. The actor cannot sign themselves out via
    this endpoint (the self-service ``POST /me/signout-everywhere``
    exists for that) — 403.
    """
    db_user = resolve_target_user_for_admin(db, current_user, user_id,
                                            "deactivate")
    if db_user is None:
        raise NotFoundError("User not found")

    if db_user.id == current_user.id:
        raise ForbiddenError("Use the sign-out-everywhere action on your "
                             "own account for yourself.")

    from services import me_auth_apikeys_service
    result = me_auth_apikeys_service.sign_out_everywhere(
        db, db_user, initiator_user_id=current_user.id)
    result["user_id"] = db_user.id
    return result


# --------------------------------------------------------------------------- #
# PATCH /{user_id}/reactivate
# --------------------------------------------------------------------------- #

def reactivate_user(db: Session, current_user: "models.User", user_id: int) -> "models.User":
    """Reactivate a previously-deactivated user. Same authorization model as
    deactivate (no self-action restriction)."""
    db_user = resolve_target_user_for_admin(db, current_user, user_id, "reactivate")

    if db_user is None:
        raise NotFoundError("User not found")

    reactivated = crud.reactivate_user(db=db, db_user=db_user)

    # Audit (ISO 27001 §A.9.2.5 — access reinstatement).
    try:
        from audit import log_permission_audit
        from models import AuditAction
        log_permission_audit(
            db=db, action=AuditAction.ADMIN_USER_MODIFY,
            actor_user_id=current_user.id, tenant_id=db_user.tenant_id,
            resource_type="user", resource_id=user_id,
            target_user_id=user_id,
        )
    except Exception:
        pass

    return reactivated


# --------------------------------------------------------------------------- #
# DELETE /{user_id}
# --------------------------------------------------------------------------- #

def delete_user_permanently(
    db: Session,
    current_user: "models.User",
    user_id: int,
    keycloak_admin_client: Any,
) -> None:
    """Permanently (soft-)delete a user from both Keycloak and the database.
    Self-deletion is rejected. `keycloak_admin_client` is passed in by the
    router (`routers.users.keycloak_admin_client`) rather than imported here,
    so the router's existing patch target (`mod.keycloak_admin_client`) keeps
    working unchanged (shared rule #7).

    - Regular users: only within their own tenant.
    - Platform admins: god-mode (tenant-unscoped, include_deleted) — the SAME
      resolver `deactivate`/`reactivate` use (`resolve_target_user_for_admin`).
      BUG.B6/B7: `GET /users/?tenant_id=N` lets a platform admin list tenant N,
      but this endpoint used to scope the lookup to the ACTING admin's own
      tenant, so every listed row outside tenant 1 answered 404 "User not
      found" — the Dashboard's Delete button could never reach it. Listing,
      deactivating and deleting must agree on which rows they can name.
      Audit still attributes to the TARGET tenant (below).
    """
    if current_user.is_platform_admin:
        db_user = users_crud.get_user_include_deleted(db, user_id)
    else:
        db_user = crud.get_user_in_tenant(db, user_id=user_id, tenant_id=current_user.tenant_id)

    if db_user is None:
        raise NotFoundError("User not found")

    if db_user.id == current_user.id:
        raise ForbiddenError("You cannot delete yourself.")

    # Delete from Keycloak first
    if db_user.keycloak_user_id:
        try:
            keycloak_admin_client.delete_user(user_id=db_user.keycloak_user_id)
        except Exception as e:
            # Only continue if user doesn't exist in Keycloak (404)
            # Otherwise, fail the entire operation to prevent orphaned accounts
            error_msg = str(e).lower()
            if 'not found' in error_msg or '404' in error_msg:
                logger.info("User already deleted from Keycloak")
            else:
                # Critical error - don't delete from database
                logger.error("Failed to delete user from Keycloak: %s", e)
                raise ServiceError(
                    "Failed to delete user from Keycloak. The user was NOT deleted "
                    "from the database to prevent orphaned accounts.",
                    code="internal_error",
                )

    # Delete from database (only if Keycloak deletion succeeded or user didn't exist)
    target_tenant_id = db_user.tenant_id
    crud.delete_user_permanently(db=db, db_user=db_user)

    # Audit (ISO 27001 §A.9.2.6 / SOC 2 CC6.3 — termination).
    try:
        from audit import log_permission_audit
        from models import AuditAction
        log_permission_audit(
            db=db, action=AuditAction.ADMIN_USER_DELETE,
            actor_user_id=current_user.id, tenant_id=target_tenant_id,
            resource_type="user", resource_id=user_id,
            target_user_id=user_id,
        )
    except Exception:
        pass

    return


# --------------------------------------------------------------------------- #
# GET /user_accounts
# --------------------------------------------------------------------------- #

def list_user_accounts(
    db: Session, current_user: "models.User", *, skip: int, limit: int
) -> List["models.User"]:
    """All child users associated with the current user's account."""
    return crud.get_child_users(db, parent_user_id=current_user.id, skip=skip, limit=limit)
