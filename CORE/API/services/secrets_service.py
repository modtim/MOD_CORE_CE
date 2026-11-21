"""Business logic for routers/secrets.py (Gate 2b per-file service/CRUD migration).

Owns:
    * Per-secret / global-role authorization (`can_access_secret`,
      `has_global_secret_capability`) — SECURITY-SENSITIVE, moved verbatim
      from the router. `_can_access_secret` stays importable at
      `routers.secrets._can_access_secret` (other routers — nim.py,
      workflows.py — import it directly), so the router keeps a thin
      pass-through wrapper of the same name; this module holds the real body.
    * Orchestration for create/update/delete/share/unshare/request-access.

Does NOT own: secret value storage. Vault + encryption I/O already lives in
crud/secret_cud.py (re-exported as crud.create_secret / crud.update_secret /
crud.delete_secret / crud.get_decrypted_secret / crud.get_decrypted_secret_by_name),
which this module calls into unchanged — that module's on-the-wire behavior
must stay byte-identical, so it is reused rather than rewritten.

Routers map the `ServiceError` subclasses below to HTTPException; this module
never raises HTTPException itself, so it stays usable outside an HTTP context
(e.g. from another service or a background job).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

import crud
import crud.secrets as secret_crud
import models
import schemas
from services._base import NotFoundError, ServiceError

logger = logging.getLogger(__name__)


class ForbiddenError(ServiceError):
    """Caller lacks the capability/permission for this action (router maps -> 403)."""

    def __init__(self, message: str = "Forbidden", *, details: Any = None):
        super().__init__(message, code="forbidden", details=details)


class BadRequestError(ServiceError):
    """Request violates a business precondition (router maps -> 400)."""

    def __init__(self, message: str = "Bad request", *, details: Any = None):
        super().__init__(message, code="bad_request", details=details)


# =========================================================================
# Authorization — SECURITY-SENSITIVE, moved verbatim from routers/secrets.py
# =========================================================================


def has_global_secret_capability(
    db: Session, current_user: "models.User", capability: str
) -> bool:
    """True if the user can <capability> ANY secret via a custom role that holds
    the SECRET capability (the legacy role-based gate). No platform-admin bypass
    (ISO/DoD tenant isolation)."""
    if current_user.custom_role_id:
        role = secret_crud.get_custom_role_with_capabilities(db, current_user.custom_role_id)
        if role and any(
            c.resource_type == models.ResourceType.SECRET and c.name == capability
            for c in role.capabilities
        ):
            return True
    return False


def can_access_secret(
    db: Session, current_user: "models.User", secret_id: int, capability: str
) -> bool:
    """Per-secret authorization. Honors BOTH the per-secret grants made in the
    Permissions UI (owner via grant_owner_permissions, direct share, team/role on
    the resource) AND the legacy global role capability. Without this, sharing a
    secret to a user had no effect."""
    if has_global_secret_capability(db, current_user, capability):
        return True
    from resource_permissions import check_resource_permission

    return check_resource_permission(
        db, current_user, models.ResourceType.SECRET, capability, secret_id
    )


# =========================================================================
# CRUD orchestration
# =========================================================================


def create_secret(
    db: Session, secret: "schemas.SecretCreate", current_user: "models.User"
) -> "models.Secret":
    """Validate the reserved-name gate, persist via crud.create_secret, and
    best-effort grant owner permissions.

    Raises `ForbiddenError` for a reserved name (after auditing the rejection).
    Lets `sqlalchemy.exc.IntegrityError` from crud.create_secret propagate
    unchanged — the router maps it to 409 (a duplicate secret name).
    """
    from validators.platform_secret_names import (
        ReservedSecretNameError,
        assert_secret_name_allowed,
    )

    try:
        assert_secret_name_allowed(
            secret.name,
            is_platform_admin=bool(getattr(current_user, "is_platform_admin", False)),
        )
    except ReservedSecretNameError as e:
        # Audit the rejection so we can spot tenant admins probing
        # the reserved-name registry.
        try:
            db.add(
                models.AuditEvent(
                    tenant_id=current_user.tenant_id,
                    user_id=current_user.id,
                    event_name="SECRET_NAME_RESERVED_BLOCKED",
                    details={"name": secret.name},
                )
            )
            db.commit()
        except Exception:
            db.rollback()
        raise ForbiddenError(str(e)) from None

    # FX.117 p4c (gov wrap) — under a gov/DoD compliance profile every tenant
    # secret must carry an expiry <= secrets.gov_max_lifetime_days, so the
    # create payload is validated BEFORE anything is written (no half-
    # encrypted secret, no Vault put). GovExpiryError -> 422 at the router.
    # A non-gov install is unchanged: expires_at stays optional.
    from services import secret_expiry_service as ses
    ses.assert_gov_expiry(
        db,
        current_user.tenant_id,
        getattr(secret, "expires_at", None),
        secret_name=secret.name,
        action="Creating",
    )

    result = crud.create_secret(db=db, secret=secret, user=current_user)
    try:
        from resource_permissions import grant_owner_permissions

        grant_owner_permissions(
            db, current_user.tenant_id, current_user.id, models.ResourceType.SECRET, result.id
        )
        db.commit()
    except Exception:
        pass

    # FX.117 p4c — crud/secret_cud does not carry the new optional create
    # field, so the expiry is written through the p4a RECORD path
    # (services/secret_expiry_service.set_expiry: one live `secret_expiries`
    # row + the `Secret.expires_at` bag mirror + the SECRET_EXPIRY_SET audit).
    # That is what makes a create-time expiry enforceable at the run gate.
    create_exp = getattr(secret, "expires_at", None)
    if isinstance(create_exp, str):
        create_exp = ses._parse_expires_at(create_exp)
    elif create_exp is not None and not isinstance(create_exp, datetime):
        create_exp = None
    if create_exp is not None:
        if create_exp.tzinfo is None:
            create_exp = create_exp.replace(tzinfo=timezone.utc)
        ses.set_expiry(db, result, create_exp, current_user)
    return result


def list_secrets(
    db: Session,
    current_user: "models.User",
    *,
    limit: int,
    offset: int,
    sort_by: str,
    order: str,
) -> list:
    """Every tenant secret (so the builder dropdown can show them), each tagged
    with whether the caller can actually use it, so the UI can warn / offer
    "request access" instead of failing at execution time."""
    secrets = crud.get_secrets_for_tenant(
        db=db,
        tenant_id=current_user.tenant_id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        order=order,
    )
    global_view = has_global_secret_capability(db, current_user, "view")
    out = []
    for s in secrets:
        s.has_access = True if global_view else can_access_secret(db, current_user, s.id, "view")
        out.append(s)
    return out


def request_secret_access(
    db: Session,
    secret_id: int,
    current_user: "models.User",
    justification: Optional[str],
    background_tasks,
) -> dict:
    """Create a pending access request for a secret the caller can't use.
    The secret owner (created_by_user_id) reviews it in the Permission Requests
    inbox; approving grants the requester 'view'."""
    db_secret = crud.get_secret_by_id(db, secret_id=secret_id, tenant_id=current_user.tenant_id)
    if not db_secret:
        raise NotFoundError("Secret not found.")
    if can_access_secret(db, current_user, secret_id, "view"):
        raise BadRequestError("You already have access to this secret.")
    existing = secret_crud.find_pending_permission_request(
        db,
        requester_id=current_user.id,
        resource_type="secret",
        resource_id=secret_id,
    )
    if existing:
        raise BadRequestError("You already have a pending request for this secret.")
    req = models.PermissionRequest(
        tenant_id=current_user.tenant_id,
        requester_id=current_user.id,
        cog_id=None,
        resource_type="secret",
        resource_id=secret_id,
        requested_level=models.PermissionLevel("viewer"),
        justification=justification,
        status=models.PermissionRequestStatus.PENDING,
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    owner = None
    if getattr(db_secret, "created_by_user_id", None):
        owner = crud.get_user(db, db_secret.created_by_user_id)
    if owner and owner.email:
        try:
            from utils.email_sender import send_permission_request_email

            background_tasks.add_task(
                send_permission_request_email,
                db=db,
                tenant_id=current_user.tenant_id,
                recipient_email=owner.email,
                recipient_name=f"{owner.first_name} {owner.last_name}",
                request_details={
                    "notification_type": "new_request",
                    "requester_name": f"{current_user.first_name} {current_user.last_name}",
                    "requester_email": current_user.email,
                    "resource_type": "secret",
                    "folder_name": db_secret.name,
                    "requested_level": "viewer",
                    "justification": justification,
                    "dashboard_url": f"{__import__('os').getenv('DASHBOARD_URL', 'http://dashboard.localhost')}/#permission-requests",
                    "tenant_id": current_user.tenant_id,
                },
            )
        except Exception:
            pass
    return {"detail": "Access request submitted", "request_id": req.id, "secret_name": db_secret.name}


def get_secret(db: Session, secret_id: int, current_user: "models.User") -> "models.Secret":
    """Retrieve the metadata for a single secret. The secret value is never returned."""
    db_secret = crud.get_secret_by_id(db, secret_id=secret_id, tenant_id=current_user.tenant_id)
    if not db_secret:
        raise NotFoundError("Secret not found.")
    if not can_access_secret(db, current_user, secret_id, "view"):
        raise ForbiddenError("You don't have access to this secret.")
    return db_secret


def update_secret(
    db: Session,
    secret_id: int,
    secret_update: "schemas.SecretUpdate",
    current_user: "models.User",
) -> "models.Secret":
    """Update a secret's name, description, or value."""
    db_secret = crud.get_secret_by_id(db, secret_id=secret_id, tenant_id=current_user.tenant_id)
    if not db_secret:
        raise NotFoundError("Secret not found")
    if not can_access_secret(db, current_user, secret_id, "edit"):
        raise ForbiddenError("You don't have edit access to this secret.")

    # LDF-4 #6 — same reserved-name gate on update (rename to a
    # reserved name is also forbidden).
    new_name = getattr(secret_update, "name", None)
    if new_name and new_name != db_secret.name:
        from validators.platform_secret_names import (
            ReservedSecretNameError,
            assert_secret_name_allowed,
        )

        try:
            assert_secret_name_allowed(
                new_name,
                is_platform_admin=bool(getattr(current_user, "is_platform_admin", False)),
            )
        except ReservedSecretNameError as e:
            raise ForbiddenError(str(e)) from None

    # FX.117 PART 2 (owner Q104=A) — per-secret allowed-workflow scope.
    # `allowed_workflow_ids` present in the payload: [] or None CLEARS the
    # scope (back to "any workflow of the tenant"); a non-empty list sets
    # it. Expiry: an ISO timestamp far enough in the future, or None to
    # clear. Changing the expiry re-arms the reminder dedupe columns so
    # the owner is re-notified for the new date (soft state only — an
    # expired secret is never hard-deleted, ISO 27001).
    upd = secret_update.dict(exclude_unset=True)

    # FX.117 p4c (gov wrap) — under a gov/DoD compliance profile the secret
    # must end up WITH an expiry <= secrets.gov_max_lifetime_days, so the
    # effective post-update expiry is validated BEFORE anything is written.
    # An update that would leave the secret without an expiry (clearing it, or
    # a PATCH that never mentions one on a secret that never had one) is
    # refused 422, as is one beyond the gov ceiling. Runs before the p2
    # max_secret_age_days check so the gov refusal is the message the caller
    # sees (422, not the generic 400).
    from services import secret_expiry_service as ses
    effective_exp = (upd.get("expires_at") if "expires_at" in upd
                     else ses.get_expiry(db, db_secret.id))
    if "expires_at" in upd and isinstance(effective_exp, str):
        effective_exp = ses._parse_expires_at(effective_exp)
    ses.assert_gov_expiry(
        db,
        current_user.tenant_id,
        effective_exp,
        secret_name=db_secret.name,
        action="Updating",
    )

    if "allowed_workflow_ids" in upd:
        allowed = upd.get("allowed_workflow_ids")
        if allowed is not None and not isinstance(allowed, list):
            raise BadRequestError(
                "allowed_workflow_ids must be a list of workflow ids."
            )
        db_secret.allowed_workflow_ids = (
            [int(w) for w in allowed] if allowed else None
        )
    if "expires_at" in upd:
        exp = upd.get("expires_at")
        if exp is not None:
            if not isinstance(exp, datetime):
                try:
                    exp = datetime.fromisoformat(str(exp))
                except (TypeError, ValueError):
                    raise BadRequestError(
                        "expires_at must be an ISO-8601 timestamp."
                    )
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            from services import platform_settings as _psr
            try:
                max_age = int(_psr.get_setting(
                    "secrets.max_secret_age_days", db=db))
            except Exception:  # noqa: BLE001 — default 365
                max_age = 365
            if exp <= datetime.now(timezone.utc):
                raise BadRequestError(
                    "expires_at must be in the future."
                )
            if exp > datetime.now(timezone.utc) + timedelta(days=max_age):
                raise BadRequestError(
                    f"expires_at may be at most {max_age} days out "
                    "(secrets.max_secret_age_days)."
                )
        db_secret.expires_at = exp
        db_secret.expiry_reminders_sent = None
        db_secret.expired_notified_at = None

    # 13.5e — when the VALUE is part of this update, read the current value
    # first so we can flag only an actual CHANGE (a no-op re-write must not
    # mark stacks "restart needed"). A Vault read failure propagates here
    # exactly as it would from the write below — the update fails closed.
    new_value = getattr(secret_update, "value", None)
    value_changed = False
    if new_value is not None:
        old_value = crud.get_decrypted_secret(
            db, secret_id=secret_id, tenant_id=db_secret.tenant_id)
        value_changed = old_value != new_value

    # Stacks reference the secret BY NAME — on a rename+rotation the stored
    # refs still carry the pre-update name, so the flag keys on it.
    pre_update_name = db_secret.name
    updated = crud.update_secret(db=db, db_secret=db_secret, secret_update=secret_update)

    if value_changed:
        # Flag every stack of THIS tenant that references the secret by name
        # (`needs_redeploy=True` + one STACK_SECRET_ROTATED audit row per
        # stack, name only — never the value). Best-effort: the rotation is
        # already committed; a flagging failure logs loudly but does not
        # un-commit it.
        try:
            from services import stack_tenant_secrets

            stack_tenant_secrets.flag_stacks_for_secret_rotation(
                db, db_secret.tenant_id, pre_update_name, current_user)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "secret rotation for %s (id %s) committed but stack "
                "redeploy-flags were not set: %s",
                updated.name, secret_id, e,
            )
    return updated


def delete_secret(db: Session, secret_id: int, current_user: "models.User") -> None:
    """Soft deletes a secret."""
    db_secret = crud.get_secret_by_id(db, secret_id=secret_id, tenant_id=current_user.tenant_id)
    if not db_secret:
        raise NotFoundError("Secret not found")
    if not can_access_secret(db, current_user, secret_id, "delete"):
        raise ForbiddenError("You don't have delete access to this secret.")

    crud.delete_secret(db=db, db_secret=db_secret)


def get_decrypted_secret_value(db: Session, secret_id: int, current_user: "models.User") -> str:
    """Retrieve the decrypted value of a single secret.

    Enforces access BEFORE logging/decrypting so a denied caller is neither
    served the value nor recorded as having accessed it.
    """
    if not can_access_secret(db, current_user, secret_id, "view"):
        raise ForbiddenError("You don't have access to this secret.")

    # Audit: secret access (CMMC AU-2, SOC2)
    try:
        db.add(
            models.AuditEvent(
                tenant_id=current_user.tenant_id,
                user_id=current_user.id,
                event_name="SECRET_ACCESSED",
                details={"secret_id": secret_id},
            )
        )
        db.commit()
    except Exception as e:
        # GSEC R-144 — a failed SECRET_ACCESSED audit write must be visible, not
        # silently dropped. WARNING + Prometheus counter via the shared helper;
        # the read itself still proceeds (access was already authorised above).
        import audit
        audit._record_audit_write_failure("secret_accessed_audit", e)

    decrypted_value = crud.get_decrypted_secret(db, secret_id=secret_id, tenant_id=current_user.tenant_id)
    if decrypted_value is None:
        raise NotFoundError("Secret not found or unable to decrypt.")
    return decrypted_value


# Platform-level secrets that can be shared with tenants (read-only)
# These are infrastructure secrets managed by the platform admin
PLATFORM_SHARED_SECRETS = [
    "harbor_registry",
    "harbor_username",
    "harbor_password",
]

PLATFORM_TENANT_ID = 1


def get_decrypted_secret_value_by_name(
    db: Session,
    secret_name: str,
    principal: Optional["models.User"] = None,
    system_internal: bool = False,
    workflow_run_id: Optional[int] = None,
    node_id: Optional[str] = None,
) -> str:
    """Retrieve the decrypted value of a single secret by name.

    R-140 — grant-gated, same semantics as the by-id path. `principal` is
    the acting user: the interactive user or API-key owner on request
    paths, or the workflow run owner / stack owner whose token the system
    is acting for on internal paths (worker credential injection calls
    the by-name endpoint with the run owner's exchanged token, so the
    run owner IS the principal and is grant-checked like any user).
    Knowing a secret's name no longer grants value access: same-tenant
    secrets require the same `can_access_secret` view grant the by-id
    path enforces, and a denial is audited (SECRET_ACCESS_DENIED) before
    the ForbiddenError propagates.

    `system_internal=True` is the explicit, named path for an internal
    system caller with no per-secret grant to enforce: the grant check is
    skipped but the read is still audited with `system_internal: true`
    in the details, so the skip is never silent. (No production caller
    uses it today — the router path always passes the acting user.)

    Lookup order for platform shared secrets (harbor_*):
    - Check the user's tenant first, then the platform tenant

    Platform-shared secrets served cross-tenant (a tenant != 1 principal
    reading a tenant-1 secret) are the designed read-only infrastructure
    share: per-tenant grants cannot exist across tenants, so that
    fallback stays available to authenticated tenant members and every
    access is audited with the principal's identity (source:
    platform_shared) — an explicit, audited share, not a bypass.

    For all other secrets: only check user's tenant
    """
    if principal is None and not system_internal:
        raise ForbiddenError(
            "A principal is required to read a secret value by name."
        )

    tenant_id = principal.tenant_id if principal is not None else None

    if secret_name in PLATFORM_SHARED_SECRETS:
        # Platform-shared secrets: check the user's tenant first, then the
        # platform tenant.
        decrypted_value = None
        if tenant_id and tenant_id != PLATFORM_TENANT_ID:
            # Plan allows custom registry - check user's tenant first.
            # R-140: resolve the secret ROW before decrypting so the grant
            # gate runs first — deny-before-decrypt, like the by-id path.
            db_secret = crud.get_secret_by_name(
                db, secret_name, tenant_id=tenant_id
            )
            if db_secret is not None:
                _authorize_secret_by_name_read(
                    db, principal, db_secret, secret_name,
                    tenant_id, system_internal,
                    workflow_run_id=workflow_run_id,
                )
                decrypted_value = crud.get_decrypted_secret_by_name(
                    db, secret_name=secret_name, tenant_id=tenant_id
                )

        # Fall back to platform tenant (or use it directly if plan doesn't
        # allow custom). A row that exists but fails the grant gate raises
        # above — no silent fall-back to a different secret.
        if decrypted_value is None:
            db_secret = crud.get_secret_by_name(
                db, secret_name, tenant_id=PLATFORM_TENANT_ID
            )
            if db_secret is not None:
                _authorize_secret_by_name_read(
                    db, principal, db_secret, secret_name,
                    PLATFORM_TENANT_ID, system_internal,
                    workflow_run_id=workflow_run_id,
                )
                decrypted_value = crud.get_decrypted_secret_by_name(
                    db, secret_name=secret_name, tenant_id=PLATFORM_TENANT_ID
                )

        if decrypted_value is None:
            raise NotFoundError(
                f"Secret '{secret_name}' not found or unable to decrypt."
            )
        return decrypted_value
    else:
        if tenant_id is None:
            raise NotFoundError(
                f"Secret '{secret_name}' not found or unable to decrypt."
            )
        # Regular secrets - only check user's tenant
        db_secret = crud.get_secret_by_name(db, secret_name, tenant_id=tenant_id)
        if db_secret is None:
            raise NotFoundError(
                f"Secret '{secret_name}' not found or unable to decrypt."
            )
        _authorize_secret_by_name_read(
            db, principal, db_secret, secret_name, tenant_id, system_internal,
            workflow_run_id=workflow_run_id,
        )
        decrypted_value = crud.get_decrypted_secret_by_name(
            db, secret_name=secret_name, tenant_id=tenant_id
        )
        if decrypted_value is None:
            raise NotFoundError(
                f"Secret '{secret_name}' not found or unable to decrypt."
            )
        return decrypted_value


def _authorize_secret_by_name_read(
    db: Session,
    principal: Optional["models.User"],
    db_secret: "models.Secret",
    secret_name: str,
    serving_tenant_id: int,
    system_internal: bool,
    workflow_run_id: Optional[int] = None,
) -> None:
    """R-140 — the per-secret gate for the by-name read path.

    Raises ForbiddenError (after auditing the denial) when the acting
    principal lacks a view grant on a same-tenant secret. The
    platform-shared cross-tenant fallback is the designed read-only
    share and is audited instead. Successful reads are audited the same
    way the by-id path audits them (SECRET_ACCESSED).

    FX.117 PART 2 (owner Q104=A): before the grant check, the run
    context (workflow_run_id, from the worker run-token injection) is
    held to the per-secret allowed-workflow scope and expiry —
    `_check_run_context_allowed` (403 + audited denial when refused).
    """
    _check_run_context_allowed(
        db, db_secret, secret_name, workflow_run_id=workflow_run_id,
    )
    # principal None (principal-less system_internal read) has no tenant
    # of its own, so a tenant-1-serving read is unambiguously the
    # cross-tenant platform share as well.
    cross_tenant_platform_share = (
        serving_tenant_id == PLATFORM_TENANT_ID
        and (principal is None or principal.tenant_id != PLATFORM_TENANT_ID)
    )
    if cross_tenant_platform_share:
        _write_secret_access_audit(
            db, principal, db_secret, secret_name,
            system_internal=system_internal, source="platform_shared",
        )
        return

    if not system_internal:
        if not can_access_secret(db, principal, db_secret.id, "view"):
            _audit_secret_access_denied(db, principal, db_secret, secret_name)
            raise ForbiddenError(
                f"You don't have access to secret '{secret_name}'."
            )

    _write_secret_access_audit(
        db, principal, db_secret, secret_name,
        system_internal=system_internal,
        source="system_internal" if system_internal else "grant",
    )


def _write_secret_access_audit(
    db: Session,
    principal: Optional["models.User"],
    db_secret: "models.Secret",
    secret_name: str,
    *,
    system_internal: bool,
    source: str,
) -> None:
    """Audit an authorised by-name secret read (CMMC AU-2, SOC2) — same
    SECRET_ACCESSED event as the by-id path; `details.source` records
    which gate authorised it. Never raises: a failed audit write is
    surfaced via the shared failure counter (GSEC R-144) and the read
    itself was already authorised above."""
    try:
        db.add(
            models.AuditEvent(
                tenant_id=principal.tenant_id if principal is not None else None,
                user_id=principal.id if principal is not None else None,
                event_name="SECRET_ACCESSED",
                details={
                    "secret_id": db_secret.id,
                    "secret_name": secret_name,
                    "by_name": True,
                    "source": source,
                    "system_internal": system_internal,
                },
            )
        )
        db.commit()
    except Exception as e:
        import audit
        audit._record_audit_write_failure("secret_accessed_by_name_audit", e)
        try:
            db.rollback()
        except Exception:
            pass


def _audit_secret_access_denied(
    db: Session,
    principal: Optional["models.User"],
    db_secret: "models.Secret",
    secret_name: str,
    reason: Optional[str] = None,
    tenant_id: Optional[int] = None,
) -> None:
    """Audit a DENIED by-name secret read before the ForbiddenError
    propagates. `reason` (FX.117 PART 2) records WHY — e.g.
    'run_workflow_not_in_scope' or 'secret_expired'; `tenant_id`
    attributes the denial when there is no acting principal (the
    run-context gate audits under the run's tenant). Never raises."""
    try:
        details: dict = {
            "secret_id": db_secret.id,
            "secret_name": secret_name,
        }
        if reason:
            details["reason"] = reason
        db.add(
            models.AuditEvent(
                tenant_id=(principal.tenant_id if principal is not None
                           else tenant_id),
                user_id=principal.id if principal is not None else None,
                event_name="SECRET_ACCESS_DENIED",
                details=details,
            )
        )
        db.commit()
    except Exception as e:
        import audit
        audit._record_audit_write_failure("secret_access_denied_audit", e)
        try:
            db.rollback()
        except Exception:
            pass


def _check_run_context_allowed(
    db: Session,
    db_secret: "models.Secret",
    secret_name: str,
    *,
    workflow_run_id: Optional[int],
) -> None:
    """FX.117 PART 2 (owner Q104=A) — the per-secret run-context gate for
    the by-name run-token injection path.

    Two refusal conditions, each 403 (ForbiddenError) + an audited
    SECRET_ACCESS_DENIED row (reuses `_audit_secret_access_denied`):

    * **allowed-workflow scope** — when `db_secret.allowed_workflow_ids`
      is set (not None/empty), the run it is being injected into must
      belong to one of those workflows (NULL scope = any tenant
      workflow, current behaviour).
    * **expiry** — when `db_secret.expires_at` is in the past, the
      secret is expired and injection is refused (the value is never
      hard-deleted; expiry is a soft state, ISO 27001).

    No `workflow_run_id` (a caller that does not identify its run)
    passes both checks: the scope can only be evaluated against a
    concrete run, and expiry has no run context to attach to — the
    sweep's owner reminders cover those secrets. Never raises except
    the intentional ForbiddenError.
    """
    # --- per-secret allowed-workflow scope ---
    # (JSONB list; a non-list/empty value = no scope = any workflow.)
    allowed = getattr(db_secret, "allowed_workflow_ids", None)
    if isinstance(allowed, (list, tuple)) and len(allowed) > 0:
        if not workflow_run_id:
            _audit_secret_access_denied(
                db, None, db_secret, secret_name,
                reason="run_workflow_not_in_scope",
            )
            raise ForbiddenError(
                f"Secret '{secret_name}' is scoped to specific workflows; "
                "the run context is required to inject it."
            )
        run = (db.query(models.WorkflowRun)
               .filter(models.WorkflowRun.id == workflow_run_id).first())
        if run is None or run.workflow_id not in allowed:
            _audit_secret_access_denied(
                db, None, db_secret, secret_name,
                reason="run_workflow_not_in_scope",
                tenant_id=getattr(db_secret, "tenant_id", None),
            )
            raise ForbiddenError(
                f"Secret '{secret_name}' is not allowed for this "
                "workflow run."
            )
    # --- expiry ---
    # (a NULL/absent expires_at = no expiry; a non-datetime value is
    # treated as no expiry, so mock/partial rows never false-refuse.)
    expires_at = getattr(db_secret, "expires_at", None)
    if isinstance(expires_at, datetime):
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            _audit_secret_access_denied(
                db, None, db_secret, secret_name,
                reason="secret_expired",
            )
            raise ForbiddenError(
                f"Secret '{secret_name}' has expired."
            )


def run_secret_names_for_run(db: Session, workflow_run_id: Optional[int]) -> list:
    """FX.117 (Q104=A) — secret names referenced by the nodes of a run's
    workflow version (from `graph_json[].data.secret_refs`). Never raises:
    a graph/version lookup failure returns []."""
    if not workflow_run_id:
        return []
    try:
        run = (db.query(models.WorkflowRun)
               .filter(models.WorkflowRun.id == workflow_run_id).first())
        if not run or not run.workflow_version_id:
            return []
        ver = (db.query(models.WorkflowVersion)
               .filter(models.WorkflowVersion.id == run.workflow_version_id)
               .first())
        if not ver or not isinstance(ver.graph_json, dict):
            return []
        names = set()
        for _node in (ver.graph_json.get("nodes") or []):
            for _ref in (((_node.get("data") or {}).get("secret_refs")) or []):
                _name = _ref.get("secret_name") if isinstance(_ref, dict) else None
                if _name:
                    names.add(_name)
        return sorted(names)
    except Exception:  # noqa: BLE001 — masking must never break the report
        return []


def masked_secret_values_for_run(db: Session, workflow_run_id: Optional[int]) -> list:
    """FX.117 (Q104=A) — plaintext values of the run's referenced secrets,
    for exact-value masking of node logs/outputs/errors the API stores.

    STRICTLY tenant-scoped: every name resolves against the run's own
    tenant only — the consuming node can never pull another tenant's value.
    Never raises: a lookup/decrypt failure yields the partial list
    collected so far (or []), degrading masking, never the task report.
    """
    if not workflow_run_id:
        return []
    try:
        run = (db.query(models.WorkflowRun)
               .filter(models.WorkflowRun.id == workflow_run_id).first())
    except Exception:  # noqa: BLE001
        return []
    if not run or not run.tenant_id:
        return []
    values = []
    for _name in run_secret_names_for_run(db, workflow_run_id):
        try:
            _v = crud.get_decrypted_secret_by_name(
                db, _name, tenant_id=run.tenant_id)
        except Exception:  # noqa: BLE001
            _v = None
        if _v:
            values.append(_v)
    return values


def run_secrets_expiry_sweep(db: Session) -> dict:
    """FX.117 PART 2 (owner Q104=A) — daily secret-expiry sweep
    (services/scheduler.py, 04:32 UTC).

    For every live secret with an expires_at:
      * at each day in the `secrets.expiry_reminder_days` platform
        setting still before expiry, notify the owner
        (created_by_user_id) via the notifications mechanism —
        deduped via secrets.expiry_reminders_sent (a JSONB list of
        already-sent days), so each day fires at most once;
      * once past expiry, mark it (expired_notified_at stamp) and
        notify the owner that it is expired. Marking is SOFT state
        only — the row is never deleted (ISO 27001), and the read
        path refuses injection of expired secrets independently
        (403 + audited, see `_check_run_context_allowed`).

    Returns counters; never raises per-secret (a failed notification
    is logged and the dedupe stamp is NOT written, so the next sweep
    retries)."""
    from services import platform_settings as psr
    now = datetime.now(timezone.utc)
    try:
        reminder_days = psr.get_setting(
            "secrets.expiry_reminder_days", db=db)
    except Exception:  # noqa: BLE001 — default [3, 14]
        reminder_days = [3, 14]
    if not isinstance(reminder_days, list):
        reminder_days = [3, 14]

    expired = 0
    reminders = 0
    secrets = (
        db.query(models.Secret)
        .filter(models.Secret.deleted_at.is_(None))
        .filter(models.Secret.expires_at.isnot(None))
        .all()
    )
    for sec in secrets:
        exp = sec.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        owner_id = sec.created_by_user_id
        try:
            if exp <= now:
                if sec.expired_notified_at is None:
                    sec.expired_notified_at = now
                    if owner_id is not None:
                        try:
                            db.add(models.Notification(
                                recipient_user_id=owner_id,
                                notification_type="secret_expired",
                                message=(f"Secret '{sec.name}' has "
                                         f"expired. It will no longer "
                                         f"be injected into workflow "
                                         f"runs. Replace it (PATCH "
                                         f"/secrets/{sec.id}) to restore "
                                         f"run-time access."),
                            ))
                        except Exception as exc:  # noqa: BLE001
                            db.rollback()
                            logger.warning(
                                "FX.117 PART 2: expired notify failed "
                                "for secret %s: %s", sec.id, exc)
                            continue
                    expired += 1
            else:
                sent = sec.expiry_reminders_sent or []
                for day in reminder_days:
                    if not isinstance(day, int) or day < 0:
                        continue
                    if day in sent:
                        continue
                    days_left = (exp - now).total_seconds() / 86400.0
                    # A 2-day band per day: the reminder fires once the
                    # secret is within a day of the N-days mark (and is
                    # not so far past it that it is already the next,
                    # nearer reminder's window). Dedupe above guarantees
                    # each day fires at most once.
                    if day - 1 < days_left <= day + 1:
                        if owner_id is not None:
                            try:
                                db.add(models.Notification(
                                    recipient_user_id=owner_id,
                                    notification_type="secret_expiring",
                                    message=(f"Secret '{sec.name}' "
                                             f"expires on "
                                             f"{exp.date().isoformat()} "
                                             f"({max(int(days_left), 0)} "
                                             f"days). Replace it "
                                             f"(PATCH /secrets/{sec.id}) "
                                             f"to avoid a run-time "
                                             f"injection outage."),
                                ))
                            except Exception as exc:  # noqa: BLE001
                                db.rollback()
                                logger.warning(
                                    "FX.117 PART 2: expiry reminder "
                                    "failed for secret %s: %s",
                                    sec.id, exc)
                                continue
                        sent.append(day)
                        sec.expiry_reminders_sent = sorted(sent)
                        reminders += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("FX.117 PART 2: expiry sweep failed for "
                           "secret %s: %s", sec.id, exc)
    if expired or reminders:
        db.commit()
    return {"expired_marked": expired, "reminders_sent": reminders,
            "checked": len(secrets)}


def audit_secret_injection(
    db: Session,
    principal: Optional["models.User"],
    db_secret: "models.Secret",
    secret_name: str,
    *,
    workflow_run_id: Optional[int] = None,
    node_id: Optional[str] = None,
) -> None:
    """FX.117 (Q104=A) — one PermissionAuditLog row (action
    SECRET_INJECTED) per secret USE at run-time injection: who
    (actor_user_id / tenant_id), what (workflow_run_id + node_id, both
    optional — the worker supplies them when it knows its own run/node),
    and which secret (resource_id / name).

    The secret VALUE is NEVER written here or anywhere in the row. Never
    raises: an audit-write failure is surfaced via the shared R-144
    failure counter, and the value has already been authorised + served
    by the caller, so the read itself still proceeds.
    """
    if principal is None or principal.id is None:
        return
    try:
        import audit
        audit.log_permission_audit(
            db=db,
            action=models.AuditAction.SECRET_INJECTED,
            actor_user_id=principal.id,
            tenant_id=principal.tenant_id,
            resource_type="secret",
            resource_id=db_secret.id if db_secret else 0,
            metadata={
                "secret_id": db_secret.id if db_secret else None,
                "secret_name": secret_name,
                "workflow_run_id": workflow_run_id,
                "node_id": node_id,
                "path": "run_token_injection",
            },
        )
    except Exception as e:
        import audit
        audit._record_audit_write_failure("secret_injection_audit", e)
        try:
            db.rollback()
        except Exception:
            pass


# =========================================================================
# GF.C0b.1 — per-tenant HuggingFace token
# =========================================================================

# A per-tenant HF token needs NO new column or discriminator: it is a plain
# tenant Secret under this well-known name (same convention as the reserved
# platform names in validators/platform_secret_names.py — case matters here
# since crud.get_secret_by_name/get_decrypted_secret_by_name do an exact
# match). This mirrors the "HF_TOKEN" env var name already used everywhere
# a HuggingFace token is threaded through (services/model_catalog_stacks.py,
# services/managed_ai_stacks.py, the node wizard's env-var extraction) — a
# tenant creates a Secret named exactly "HF_TOKEN" via the normal
# POST /secrets endpoint (crud.create_secret) and it is picked up here.
# Not added to PLATFORM_RESERVED_SECRET_NAMES: tenants MUST be able to set
# their own "HF_TOKEN" secret, that's the whole point of this helper.
HF_TOKEN_SECRET_NAME = "HF_TOKEN"


def get_tenant_hf_token(db: Session, tenant_id: int) -> Optional[str]:
    """Return tenant_id's own HuggingFace token (decrypted from Vault), or
    None if they have not set one.

    Strictly tenant-scoped — unlike get_decrypted_secret_value_by_name's
    PLATFORM_SHARED_SECRETS path, this never falls back to another tenant's
    secret or a platform-shared one. Callers that need the platform's
    account-wide token for gated model fetches already use
    services/model_catalog_stacks.HF_TOKEN_VAULT_PATH directly; this helper
    is only for a tenant's own BYO token. Never logs the value.
    """
    return crud.get_decrypted_secret_by_name(
        db, secret_name=HF_TOKEN_SECRET_NAME, tenant_id=tenant_id
    )


# =========================================================================
# Vendor scanner credential helper (Fortress 3.3 S4)
# =========================================================================

VENDOR_SCANNER_SECRET_NAMES: dict[str, str] = {
    "hive": "VENDOR_SCANNER_HIVE",
    "aws_comprehend": "VENDOR_SCANNER_AWS_COMPREHEND",
}


def get_vendor_scanner_key(
    db: Session, tenant_id: int, vendor: str
) -> Optional[str]:
    """Return a vendor-scanner credential for `tenant_id`, or None.

    Looks up the tenant's own secret (by name mapped from `vendor`),
    decrypts it from Vault, and returns the plaintext value.

    Strictly tenant-scoped — never falls back to another tenant's secret
    or a platform-shared one (same isolation as `get_tenant_hf_token`).
    Unknown vendors return None without hitting the DB or Vault.
    Never logs the value.
    """
    secret_name = VENDOR_SCANNER_SECRET_NAMES.get(vendor)
    if secret_name is None:
        return None

    return crud.get_decrypted_secret_by_name(
        db, secret_name=secret_name, tenant_id=tenant_id
    )


# =========================================================================
# Platform secret sharing — admin shares secrets with tenants
# =========================================================================


def share_secret_with_tenants(
    db: Session,
    secret_id: int,
    tenant_ids: Optional[list],
    all_tenants: bool,
    current_user: "models.User",
) -> "models.Secret":
    """Share a platform secret with specific tenants or all tenants. Platform admin only."""
    if not current_user.is_platform_admin:
        raise ForbiddenError("Only platform admins can share secrets")

    secret = secret_crud.get_platform_secret_by_id(db, secret_id)
    if not secret:
        raise NotFoundError("Secret not found")

    if secret.tenant_id is not None:
        raise BadRequestError("Only platform secrets (tenant_id=NULL) can be shared")

    if all_tenants:
        secret.shared_with_all_tenants = True
        secret.shared_with_tenant_ids = None
    elif tenant_ids:
        for tid in tenant_ids:
            tenant = secret_crud.get_tenant_by_id(db, tid)
            if not tenant:
                raise NotFoundError(f"Tenant {tid} not found")

        existing = secret.shared_with_tenant_ids or []
        merged = list(set(existing + [str(tid) for tid in tenant_ids]))
        secret.shared_with_tenant_ids = merged
        secret.shared_with_all_tenants = False
    else:
        raise BadRequestError("Provide tenant_ids or set all_tenants=true")

    db.commit()
    return secret


def unshare_secret(
    db: Session,
    secret_id: int,
    tenant_ids: Optional[list],
    current_user: "models.User",
) -> "models.Secret":
    """Revoke sharing of a platform secret. Platform admin only.

    If tenant_ids provided, removes only those tenants.
    If no tenant_ids, revokes all sharing.
    """
    if not current_user.is_platform_admin:
        raise ForbiddenError("Only platform admins can unshare secrets")

    secret = secret_crud.get_platform_secret_by_id(db, secret_id)
    if not secret:
        raise NotFoundError("Secret not found")

    if tenant_ids:
        existing = secret.shared_with_tenant_ids or []
        remaining = [tid for tid in existing if tid not in [str(t) for t in tenant_ids]]
        secret.shared_with_tenant_ids = remaining if remaining else None
    else:
        secret.shared_with_all_tenants = False
        secret.shared_with_tenant_ids = None

    db.commit()
    return secret


def list_shared_secrets(db: Session, current_user: "models.User") -> list:
    """List all platform secrets that are shared with tenants. Platform admin only."""
    if not current_user.is_platform_admin:
        raise ForbiddenError("Only platform admins can view shared secrets")

    return secret_crud.list_shared_platform_secrets(db)
