"""GX.S1b (launch-completion-megaplan Group X) — app user types + app users.

An app declares its own user TYPES (e.g. `consignor`). Those users
authenticate through the platform IdP via the APP's KC client (GX.A2, SAME
realm — see the design note in
the internal design notes) and are
members of KC group ``app:<slug>:<type>``. They are ADDITIVE and outside
MOD:

  * they must NEVER get a `users` row — that invariant is what keeps them
    out of every MOD route (the central refusal lives in
    dependencies.get_current_user → 403 APP_USER_NO_PLATFORM_ACCESS);
  * the app learns their type through forward-auth's `X-Mod-User-Type`
    (the verify endpoint will call :func:`app_user_type_for` — the lead
    wires it, see the TODO(GX.S1b-wire) note);
  * management (define types, invite / self-signup, list / soft-disable,
    audit) is tenant-admin only (routers/app_user_types.py).

ISO 27001: disable is SOFT (status + disabled_at); the KC user is never
deleted (it may belong to other apps) — at most de-grouped.

KC admin calls go through the ``keycloak_admin_client`` seam
(keycloak_auth.KeycloakAdmin); tests mock the ``kc_*`` helpers in THIS
module, never live Keycloak.
"""
from __future__ import annotations
from utils.pii_redaction import mask_email


import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from keycloak_auth import keycloak_admin_client

logger = logging.getLogger(__name__)

# Machine key for a user type: lowercase alnum start, then alnum/_/-, <=63.
_TYPE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# KC required actions for an invited app user (first login: pick a password,
# confirm the email).
_INVITE_REQUIRED_ACTIONS = ["UPDATE_PASSWORD", "VERIFY_EMAIL"]


def group_name_for(app: models.App, type_name: str) -> str:
    """The KC group an app user of this type belongs to (shared realm)."""
    return f"app:{app.slug}:{type_name}"


# ---------------------------------------------------------------------------
# KC admin seam (mocked by tests — never live Keycloak in unit tests)
# ---------------------------------------------------------------------------

def _kc_headers():
    token = keycloak_admin_client._get_token()
    return (
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        keycloak_admin_client.admin_api_url,
    )


def kc_find_user_by_email(email: str) -> Optional[dict]:
    """Keycloak: exact-match user lookup by email (None when absent)."""
    headers, base = _kc_headers()
    r = httpx.get(f"{base}/users", headers=headers,
                  params={"email": email, "exact": "true"}, timeout=15)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def kc_create_user(email: str) -> str:
    """Keycloak: create a realm user for an invited app user.

    Starts DISABLED-for-login in the practical sense: enabled in the
    directory but with requiredActions UPDATE_PASSWORD + VERIFY_EMAIL, so
    the first interactive login forces both (no password exists yet).
    Returns the KC ``sub``.
    """
    headers, base = _kc_headers()
    payload = {
        "email": email,
        "username": email,
        "enabled": True,
        "emailVerified": False,
        "requiredActions": list(_INVITE_REQUIRED_ACTIONS),
    }
    r = httpx.post(f"{base}/users", headers=headers, json=payload, timeout=15)
    r.raise_for_status()
    loc = r.headers.get("Location") or ""
    sub = loc.rstrip("/").split("/")[-1]
    if not sub:
        raise HTTPException(status_code=502,
                            detail="Keycloak did not return a user id.")
    logger.info("app-user: created KC user for %s", mask_email(email))
    return sub


def kc_ensure_group(group_name: str) -> str:
    """Keycloak: idempotently create-or-find the (top-level) group, return id."""
    headers, base = _kc_headers()
    r = httpx.get(f"{base}/groups", headers=headers,
                  params={"search": group_name, "exact": "true"}, timeout=15)
    r.raise_for_status()
    for g in r.json():
        if g.get("name") == group_name:
            return g["id"]
    r = httpx.post(f"{base}/groups", headers=headers,
                   json={"name": group_name}, timeout=15)
    if r.status_code != 201:
        r.raise_for_status()
    loc = r.headers.get("Location") or ""
    gid = loc.rstrip("/").split("/")[-1]
    logger.info("app-user: created KC group %s", group_name)
    return gid


def kc_assign_user_to_group(kc_sub: str, group_name: str) -> bool:
    """Keycloak: add the user to the group (existing admin helper)."""
    try:
        return keycloak_admin_client.assign_user_to_group(kc_sub, group_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("app-user: KC group add failed sub=%s group=%s: %s",
                       kc_sub, group_name, e)
        return False


def kc_remove_user_from_group(kc_sub: str, group_name: str) -> bool:
    """Keycloak: remove the user from the group (best-effort; the KC user
    itself is NEVER deleted — it may belong to other apps)."""
    try:
        headers, base = _kc_headers()
        r = httpx.get(f"{base}/groups", headers=headers,
                      params={"search": group_name, "exact": "true"}, timeout=15)
        r.raise_for_status()
        group = next((g for g in r.json() if g.get("name") == group_name), None)
        if group is None:
            return False
        d = httpx.delete(f"{base}/users/{kc_sub}/groups/{group['id']}",
                         headers=headers, timeout=15)
        if d.status_code not in (204, 404):
            d.raise_for_status()
        logger.info("app-user: removed KC user %s from group %s",
                    kc_sub, group_name)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("app-user: KC group remove failed sub=%s group=%s: %s",
                       kc_sub, group_name, e)
        return False


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

def _audit(db: Session, action: models.AuditAction, app: models.App,
           actor, details: dict) -> None:
    """Append an audit row (append-only by design). Never raises — a failed
    audit must not break the management operation."""
    try:
        db.add(models.AuditEvent(
            tenant_id=app.tenant_id,
            user_id=getattr(actor, "id", None),
            event_name=action.value,
            details=details,
        ))
    except Exception as e:  # noqa: BLE001
        logger.warning("app-user: audit %s failed: %s", action, e)


# ---------------------------------------------------------------------------
# user types: define / list / soft-delete
# ---------------------------------------------------------------------------

def define_user_type(db: Session, app: models.App, actor, *, name: str,
                     label: Optional[str] = None,
                     self_signup: bool = False) -> models.AppUserType:
    """Create (or update, when the name exists) one of the app's user types.

    The type is what `X-Mod-User-Type` will carry (forward-auth, GX.S1) and
    what the invite / self-signup flows attach the KC user to.
    """
    name = (name or "").strip()
    if not _TYPE_NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail="user type name must match ^[a-z0-9][a-z0-9_-]{0,62}$",
        )
    existing = (db.query(models.AppUserType)
                .filter(models.AppUserType.app_id == app.id,
                        models.AppUserType.name == name,
                        models.AppUserType.deleted_at.is_(None))
                .first())
    if existing is not None:
        # Update-in-place: same type, new label / self-signup toggle.
        existing.label = label if label is not None else existing.label
        existing.self_signup = bool(self_signup)
        _audit(db, models.AuditAction.APP_USER_TYPE_SET, app, actor,
               {"app_id": app.id, "type": name, "change": "updated",
                "self_signup": existing.self_signup})
        db.commit()
        db.refresh(existing)
        return existing

    t = models.AppUserType(
        app_id=app.id,
        tenant_id=app.tenant_id,
        name=name,
        label=label,
        self_signup=bool(self_signup),
        created_by_user_id=getattr(actor, "id", None),
    )
    db.add(t)
    _audit(db, models.AuditAction.APP_USER_TYPE_SET, app, actor,
           {"app_id": app.id, "type": name, "change": "created",
            "self_signup": bool(self_signup)})
    db.commit()
    db.refresh(t)
    return t


def update_user_type(db: Session, app: models.App, actor, type_id: int,
                     *, label: Optional[str] = None,
                     self_signup: Optional[bool] = None) -> models.AppUserType:
    """Edit a type's label / self-signup toggle (tenant-isolated)."""
    t = _get_type(db, app, type_id)
    if label is not None:
        t.label = label
    if self_signup is not None:
        t.self_signup = bool(self_signup)
    _audit(db, models.AuditAction.APP_USER_TYPE_SET, app, actor,
           {"app_id": app.id, "type": t.name, "change": "updated",
            "self_signup": t.self_signup})
    db.commit()
    db.refresh(t)
    return t


def soft_delete_user_type(db: Session, app: models.App, actor,
                          type_id: int) -> models.AppUserType:
    """ISO 27001: SOFT-delete the type (deleted_at). Rows in app_users keep
    their denormalised type name but app_user_type_for() stops reporting
    them (the type no longer exists). Never hard-deleted."""
    t = _get_type(db, app, type_id)
    t.deleted_at = datetime.now(timezone.utc)
    _audit(db, models.AuditAction.APP_USER_TYPE_SET, app, actor,
           {"app_id": app.id, "type": t.name, "change": "soft_deleted"})
    db.commit()
    db.refresh(t)
    return t


def list_user_types(db: Session, app: models.App) -> list[models.AppUserType]:
    return (db.query(models.AppUserType)
            .filter(models.AppUserType.app_id == app.id,
                    models.AppUserType.deleted_at.is_(None))
            .order_by(models.AppUserType.name)
            .all())


def _get_type(db: Session, app: models.App, type_id: int) -> models.AppUserType:
    """Fetch a live (non soft-deleted) type OF THIS APP — 404 otherwise
    (tenant isolation: another tenant's app id or type id is invisible)."""
    t = (db.query(models.AppUserType)
         .filter(models.AppUserType.id == type_id,
                 models.AppUserType.app_id == app.id,
                 models.AppUserType.tenant_id == app.tenant_id,
                 models.AppUserType.deleted_at.is_(None))
         .first())
    if t is None:
        raise HTTPException(status_code=404, detail="app user type not found")
    return t


# ---------------------------------------------------------------------------
# app users: invite / list / soft-disable / self-signup
# ---------------------------------------------------------------------------

def invite_user(db: Session, app: models.App, actor, *, email: str,
                type_name: str) -> models.AppUser:
    """Invite one human into the app under a declared type.

    KC side (same realm): create the realm user if absent (required actions
    UPDATE_PASSWORD + VERIFY_EMAIL), ensure the group
    ``app:<slug>:<type>`` exists, assign the user to it. DB side: ONE
    app_users row — and, by contract, NO `users` row is ever created here.
    """
    email = (email or "").strip().lower()
    if "@" not in email or not email:
        raise HTTPException(status_code=422, detail="a valid email is required")

    t = (db.query(models.AppUserType)
         .filter(models.AppUserType.app_id == app.id,
                 models.AppUserType.name == type_name,
                 models.AppUserType.deleted_at.is_(None))
         .first())
    if t is None:
        raise HTTPException(status_code=404, detail="app user type not found")

    existing = (db.query(models.AppUser)
                .filter(models.AppUser.app_id == app.id,
                        models.AppUser.email == email,
                        models.AppUser.deleted_at.is_(None))
                .first())
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"user {email} already belongs to this app "
                   f"(status={existing.status})",
        )

    group = group_name_for(app, t.name)
    found = kc_find_user_by_email(email)
    if found is not None:
        kc_sub = found["id"]
    else:
        kc_sub = kc_create_user(email)
    kc_ensure_group(group)
    kc_assign_user_to_group(kc_sub, group)

    row = models.AppUser(
        app_id=app.id,
        tenant_id=app.tenant_id,
        app_user_type_id=t.id,
        kc_sub=kc_sub,
        email=email,
        user_type=t.name,
        status="active",
        invited_by=getattr(actor, "id", None),
    )
    db.add(row)
    _audit(db, models.AuditAction.APP_USER_INVITED, app, actor,
           {"app_id": app.id, "email": email, "user_type": t.name,
            "kc_sub": kc_sub})
    db.commit()
    db.refresh(row)
    return row


def list_app_users(db: Session, app: models.App) -> list[models.AppUser]:
    return (db.query(models.AppUser)
            .filter(models.AppUser.app_id == app.id,
                    models.AppUser.deleted_at.is_(None))
            .order_by(models.AppUser.created_at.desc())
            .all())


def disable_user(db: Session, app: models.App, actor, app_user_id: int
                 ) -> models.AppUser:
    """SOFT-disable an app user (ISO 27001 — never hard-deleted): the row
    flips to status='disabled' + disabled_at, and the KC user is removed
    from the app's group (best-effort; the KC user itself is NEVER deleted,
    since it may belong to other apps). Idempotent."""
    row = (db.query(models.AppUser)
           .filter(models.AppUser.id == app_user_id,
                   models.AppUser.app_id == app.id,
                   models.AppUser.tenant_id == app.tenant_id,
                   models.AppUser.deleted_at.is_(None))
           .first())
    if row is None:
        raise HTTPException(status_code=404, detail="app user not found")
    if row.status != "disabled":
        row.status = "disabled"
        row.disabled_at = datetime.now(timezone.utc)
        kc_remove_user_from_group(row.kc_sub, group_name_for(app, row.user_type))
        _audit(db, models.AuditAction.APP_USER_DISABLED, app, actor,
               {"app_id": app.id, "email": row.email,
                "user_type": row.user_type, "kc_sub": row.kc_sub})
        db.commit()
        db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# forward-auth integration (called by the S1 verify flow once the lead wires
# it — see the TODO(GX.S1b-wire) note for user_type_for in
# services/app_forward_auth.py)
# ---------------------------------------------------------------------------

def app_user_type_for(db: Session, app_id: int, kc_sub: str) -> Optional[str]:
    """The type this KC identity holds under this app, or None.

    None when: no app_users row, the row is disabled / soft-deleted, or the
    type itself was soft-deleted. Used for forward-auth's
    `X-Mod-User-Type` header.
    """
    if not kc_sub:
        return None
    row = (db.query(models.AppUser)
           .filter(models.AppUser.app_id == app_id,
                   models.AppUser.kc_sub == kc_sub,
                   models.AppUser.status == "active",
                   models.AppUser.deleted_at.is_(None))
           .first())
    if row is None:
        return None
    t = (db.query(models.AppUserType)
         .filter(models.AppUserType.id == row.app_user_type_id,
                 models.AppUserType.deleted_at.is_(None))
         .first())
    return row.user_type if t is not None else None


def sub_is_app_user(db: Session, kc_sub: str) -> bool:
    """True when this KC identity holds ANY (active or disabled, not
    soft-deleted) app_users row — for any app.

    Used by the central refusal in dependencies.get_current_user
    (APP_USER_NO_PLATFORM_ACCESS): a token whose sub is only an app user
    has no MOD identity and must be rejected on every MOD route before any
    auto-provisioning could create a `users` row.
    """
    if not kc_sub:
        return False
    row = (db.query(models.AppUser.id)
           .filter(models.AppUser.kc_sub == kc_sub,
                   models.AppUser.deleted_at.is_(None))
           .first())
    return row is not None


def self_sign_up_for(db: Session, app: models.App, kc_sub: str,
                     email: str, kc_groups) -> Optional[str]:
    """Self-signup on first login via this app (forward-auth, GX.S1 wiring).

    Only when a DECLARED type has self_signup=true AND the KC `groups` claim
    of the incoming token carries that type's group ``app:<slug>:<type>``
    (the app grants group membership through its own flow) AND no app_users
    row exists yet for (app, sub). Creates the row (invited_by NULL =
    self-signup) and returns the type name; otherwise returns None
    (no row, no error — the verify flow just reports the existing state).
    Never touches the `users` table.
    """
    if not kc_sub:
        return None
    groups = set(kc_groups or [])
    existing = (db.query(models.AppUser)
                .filter(models.AppUser.app_id == app.id,
                        models.AppUser.kc_sub == kc_sub)
                .first())
    if existing is not None:
        return None  # already invited/disabled — no (re)creation
    for t in list_user_types(db, app):
        if not t.self_signup:
            continue
        if group_name_for(app, t.name) in groups:
            row = models.AppUser(
                app_id=app.id,
                tenant_id=app.tenant_id,
                app_user_type_id=t.id,
                kc_sub=kc_sub,
                email=(email or kc_sub).strip().lower(),
                user_type=t.name,
                status="active",
                invited_by=None,
            )
            db.add(row)
            _audit(db, models.AuditAction.APP_USER_INVITED, app, None,
                   {"app_id": app.id, "email": row.email,
                    "user_type": t.name, "kc_sub": kc_sub,
                    "channel": "self_signup"})
            db.commit()
            db.refresh(row)
            return t.name
    return None
