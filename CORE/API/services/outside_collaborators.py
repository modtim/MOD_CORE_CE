"""FX.110 (owner decision Q97=A) — outside collaborators.

Tenant-scoped policy + enforcement for external users (models.User
``is_external``) and external grants (models.ResourcePermission
``is_external_user``):

* Expiry — every external account AND every external grant carries an
  end date. Default 90 days via the tenant setting
  ``external_collab.default_expiry_days``; the inviter is reminded
  ``external_collab.reminder_days_before`` (7) days before, deduped via
  ``users.external_expiry_reminded_at``; the daily scheduler job
  (services/scheduler.py FX.110 block) soft-disables expired grants and
  deactivates expired external users. NEVER a hard delete.
* Sign-in — ``enforce_external_gate`` requires the external user's
  access token to carry an MFA/SSO claim (``amr`` strong-auth value or
  a federated ``idp``), checked at the token/auth dependency level.
* Scope — ``scoped_cog_ids`` / ``scoped_workflow_ids`` restrict an
  external user's listings to the resources explicitly granted.
* Terms gate — tenant NDA text in ``external_collab.terms_text``; until
  ``users.external_terms_accepted_at`` is stamped, every API call
  except auth/terms-accept returns 403 EXTERNAL_TERMS_NOT_ACCEPTED.
  Acceptance logs an audit row with timestamp + terms version hash.
* Restriction options — per-grant ``view_only`` / ``no_download`` /
  ``forced_watermark`` (ResourcePermission.external_restriction_options)
  reusing the GY.V1 watermark delivery path.
* Access reviews — external users are listed separately from GC10
  campaigns; tenant admins can revoke all external access in one
  audited soft action.
* Gov/DoD — cmmc / fedramp_high_il5 profiles force
  ``external_collab.require_admin_approval``; each external user needs
  an explicit tenant-admin approval (users.external_profile_approved_by_user_id)
  before first access.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import or_, select

import models

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error codes (X-Error-Code headers on the 403s the gate returns)
# ---------------------------------------------------------------------------
ERR_ACCESS_EXPIRED = "EXTERNAL_ACCESS_EXPIRED"
ERR_ACCESS_DISABLED = "EXTERNAL_ACCESS_DISABLED"
ERR_MFA_REQUIRED = "EXTERNAL_MFA_REQUIRED"
ERR_PROFILE_APPROVAL_REQUIRED = "EXTERNAL_PROFILE_APPROVAL_REQUIRED"
ERR_TERMS_NOT_ACCEPTED = "EXTERNAL_TERMS_NOT_ACCEPTED"
ERR_NO_DOWNLOAD = "EXTERNAL_NO_DOWNLOAD"
ERR_VIEW_ONLY = "EXTERNAL_VIEW_ONLY"

# amr values that count as strong auth (MFA). Keycloak stamps the
# access token's amr claim with the authentication methods of the login
# event: "otp" (TOTP), "webauthn*" (passkeys/security keys). "pwd"
# alone is NOT strong auth.
_STRONG_AMR = {"otp", "totp", "mfa", "webauthn",
               "webauthn:client-attested", "webauthn:client-not-attested"}

RESTRICTION_KEYS = ("view_only", "no_download", "forced_watermark")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Make a stored timestamp aware-UTC for comparisons (NULL-safe)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Policy resolution (tenant row > platform row > default; profile force)
# ---------------------------------------------------------------------------

def _profile_requires_admin_approval(db) -> bool:
    """FX.110: True when an ACTIVE compliance profile (cmmc /
    fedramp_high_il5) requires external_collab.require_admin_approval.
    Same re-derivation pattern as FX.102's share.public_links_enabled
    force — a drifted settings value can never reopen external access
    on a gov/DoD install. Never raises."""
    try:
        # FX.162.p5e — CE carve: no profiles module -> nothing forced.
        from utils.paid_import import optional_paid
        cprof = optional_paid("services.compliance_profiles")
        profiles = (cprof.active_profiles(db=db) if cprof is not None else [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("FX.110: active profile lookup failed (%s)", exc)
        return False
    for profile in profiles or []:
        entry = (cprof.PROFILE_REQUIRED_SETTINGS.get(profile) or {}).get(
            "external_collab.require_admin_approval")
        if entry is not None and entry.get("req") == "true":
            return True
    return False


def get_policy(db, tenant_id: Optional[int]) -> Dict[str, Any]:
    """Resolve the tenant's outside-collaborator policy."""
    from services import platform_settings as psr
    require_approval = bool(psr.get_setting(
        "external_collab.require_admin_approval", db=db, tenant_id=tenant_id))
    if _profile_requires_admin_approval(db):
        require_approval = True
    return {
        "default_expiry_days": int(psr.get_setting(
            "external_collab.default_expiry_days", db=db, tenant_id=tenant_id)),
        "reminder_days_before": int(psr.get_setting(
            "external_collab.reminder_days_before", db=db, tenant_id=tenant_id)),
        "require_mfa": bool(psr.get_setting(
            "external_collab.require_mfa", db=db, tenant_id=tenant_id)),
        "terms_text": str(psr.get_setting(
            "external_collab.terms_text", db=db, tenant_id=tenant_id) or ""),
        "require_admin_approval": require_approval,
    }


# ---------------------------------------------------------------------------
# Sign-in + first-access gate (token/auth dependency level)
# ---------------------------------------------------------------------------

def token_has_strong_auth(claims: Optional[Dict[str, Any]]) -> bool:
    """True when the token proves MFA (amr strong-auth value) or the
    user's own-company SSO (federated login — Keycloak stamps the
    ``idp`` claim on the access token of federated logins)."""
    if not claims:
        return False
    amr = claims.get("amr") or []
    if isinstance(amr, str):
        amr = [amr]
    if any(str(a) in _STRONG_AMR for a in amr):
        return True
    # Federated SSO: idp present = logged in through the company IdP.
    if claims.get("idp"):
        return True
    return False


def terms_version_hash(db, tenant_id: Optional[int]) -> str:
    """SHA-256 over the tenant's current terms text — the 'version'
    recorded on acceptance (an audit row stores hash + timestamp)."""
    policy = get_policy(db, tenant_id)
    return hashlib.sha256(policy["terms_text"].encode("utf-8")).hexdigest()


def enforce_external_gate(db, user, claims: Optional[Dict[str, Any]] = None,
                          *, require_terms: bool = True) -> None:
    """FX.110 first-access gate for external users. Called from the auth
    dependency (dependencies.get_current_user) so EVERY API call passes
    through it; internal users pass untouched. Raises HTTPException(403)
    with an X-Error-Code header on failure. ``require_terms=False`` is
    used ONLY by the terms-accept endpoint itself (the accept must be
    reachable while terms are unaccepted)."""
    from fastapi import HTTPException, status

    if not getattr(user, "is_external", False):
        return

    # 1. Account expiry (soft-disabled access, audited by the daily job).
    exp = _aware(getattr(user, "external_access_expires_at", None))
    if exp is not None and _now() >= exp:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your external access has expired. Contact the tenant "
                   "admin who invited you to be re-invited.",
            headers={"X-Error-Code": ERR_ACCESS_EXPIRED})

    # 2. Disabled account (deactivated by expiry job / admin remove-all).
    if getattr(user, "status", None) != models.UserStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your external access has been disabled by the tenant "
                   "admin.",
            headers={"X-Error-Code": ERR_ACCESS_DISABLED})

    policy = get_policy(db, user.tenant_id)

    # 3. Sign-in strength: MFA or the user's own company SSO.
    if policy["require_mfa"] and not token_has_strong_auth(claims):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="External collaborators must sign in with MFA or their "
                   "own company SSO. Please log out and sign in again "
                   "using MFA or your company single sign-on.",
            headers={"X-Error-Code": ERR_MFA_REQUIRED})

    # 4. Gov/DoD per-user approval (profile-forced or tenant setting).
    if policy["require_admin_approval"] and \
            getattr(user, "external_profile_approved_by_user_id", None) is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Under this tenant's compliance policy, your external "
                   "access requires approval by a tenant administrator. "
                   "Your access has not been approved yet.",
            headers={"X-Error-Code": ERR_PROFILE_APPROVAL_REQUIRED})

    # 5. Terms gate — every API call except auth/accept.
    if require_terms and policy["terms_text"] and \
            getattr(user, "external_terms_accepted_at", None) is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must accept this tenant's collaboration terms "
                   "(NDA) before accessing any resource.",
            headers={"X-Error-Code": ERR_TERMS_NOT_ACCEPTED})


def accept_terms(db, user, *, claims: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Stamp terms acceptance + audit row (timestamp + version hash).
    Idempotent: re-accepting refreshes the timestamp (a changed terms
    text yields a different version hash on the new audit row)."""
    policy = get_policy(db, user.tenant_id)
    if not policy["terms_text"]:
        raise ValueError("This tenant has no collaboration terms to accept.")
    now = _now()
    user.external_terms_accepted_at = now
    version_hash = terms_version_hash(db, user.tenant_id)
    db.add(user)
    try:
        db.add(models.AuditEvent(
            tenant_id=user.tenant_id,
            user_id=user.id,
            event_name="EXTERNAL_TERMS_ACCEPTED",
            details={
                "terms_version_hash": version_hash,
                "accepted_at": now.isoformat(),
                "is_external": True,
            },
        ))
        db.commit()
    except Exception:  # noqa: BLE001 — best-effort audit, match the
        db.rollback()  # existing accept_invitation audit pattern.
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    db.refresh(user)
    return {
        "accepted": True,
        "accepted_at": now,
        "terms_version_hash": version_hash,
    }


# ---------------------------------------------------------------------------
# Scope: external users see ONLY explicitly shared resources
# ---------------------------------------------------------------------------

def _external_grants(db, user) -> list:
    """All active, unexpired external grants held by this user
    (direct or via their teams)."""
    now = _now()
    team_ids = [
        row[0] for row in db.execute(
            select(models.team_members_association.c.team_id).where(
                models.team_members_association.c.user_id == user.id)
        ).all()
    ]
    q = db.query(models.ResourcePermission).filter(
        models.ResourcePermission.tenant_id == user.tenant_id,
        models.ResourcePermission.is_external_user.is_(True),
        models.ResourcePermission.is_active.is_(True),
        models.ResourcePermission.revoked_at.is_(None),
    )
    conds = [models.ResourcePermission.user_id == user.id]
    if team_ids:
        conds.append(models.ResourcePermission.team_id.in_(team_ids))
    q = q.filter(or_(*conds))
    grants = q.all()
    return [g for g in grants
            if g.expires_at is None or _aware(g.expires_at) > now]


def scoped_cog_ids(db, user) -> Tuple[bool, Set[int]]:
    """(wildcard, ids): the cogs this external user may see. Wildcard =
    an explicit grant with resource_id NULL (share-ALL was deliberate).
    Non-external callers should not use this — normal users go through
    the standard permission-aware listing."""
    ids: Set[int] = set()
    wildcard = False
    for g in _external_grants(db, user):
        if g.resource_type not in (models.ResourceType.COG,):
            continue
        if g.resource_id is None:
            wildcard = True
        else:
            ids.add(g.resource_id)
    return wildcard, ids


def scoped_workflow_ids(db, user) -> Set[int]:
    """Workflow ids this external user may see (explicit grants only —
    workflow_view_scope was made external-aware to never return
    see_all for external users)."""
    from resource_permissions import workflow_view_scope
    see_all, ids = workflow_view_scope(db, user)
    if see_all:  # defensive: the scope fn must not say see_all for external
        return set()
    return set(ids or set())


# ---------------------------------------------------------------------------
# Per-grant restriction options (GY.V1 watermark reuse)
# ---------------------------------------------------------------------------

def grant_restrictions(db, user, resource_type, resource_id: Optional[int]) -> Dict[str, bool]:
    """Merge the MOST restrictive options across every active external
    grant of this user on the target resource (a strict grant wins over
    a loose one — flags OR together). Non-external users get all-False
    (their normal capability gating applies)."""
    out = {"view_only": False, "no_download": False, "forced_watermark": False}
    if not getattr(user, "is_external", False):
        return out
    for g in _external_grants(db, user):
        if g.resource_type != resource_type:
            continue
        if resource_id is not None and g.resource_id not in (None, resource_id):
            continue
        opts = g.external_restriction_options or {}
        if not isinstance(opts, dict):
            continue
        for k in RESTRICTION_KEYS:
            if opts.get(k):
                out[k] = True
    return out


def require_can_download(db, user, cog) -> None:
    """403 EXTERNAL_NO_DOWNLOAD when a view_only/no_download restriction
    blocks this external user from downloading the cog."""
    from fastapi import HTTPException, status
    r = grant_restrictions(db, user, models.ResourceType.COG, cog.id)
    if r["no_download"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your external access to this resource is "
                   "download-restricted by the tenant.",
            headers={"X-Error-Code": ERR_NO_DOWNLOAD})


def require_not_view_only_write(db, user, cog, action: str = "upload") -> None:
    """403 EXTERNAL_VIEW_ONLY when a view_only restriction blocks a
    write action on the cog (upload / create / modify)."""
    from fastapi import HTTPException, status
    r = grant_restrictions(db, user, models.ResourceType.COG, cog.id)
    if r["view_only"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your external access to this resource is view-only.",
            headers={"X-Error-Code": ERR_VIEW_ONLY})


def forced_watermark_required(db, user, cog) -> bool:
    """True when any active external grant on this cog forces the GY.V1
    watermarked delivery path (services/watermark /
    services/watermark_delivery) regardless of the tenant's own
    watermark policy."""
    r = grant_restrictions(db, user, models.ResourceType.COG, cog.id)
    return bool(r["forced_watermark"])


# ---------------------------------------------------------------------------
# Daily job: soft-disable expired access (never a hard delete)
# ---------------------------------------------------------------------------

def _audit(db, tenant_id, event_name, details, user_id=None):
    try:
        db.add(models.AuditEvent(
            tenant_id=tenant_id, user_id=user_id,
            event_name=event_name, details=details,
        ))
        db.commit()
    except Exception:  # noqa: BLE001 — best-effort audit
        db.rollback()
        logger.warning("FX.110: audit write failed (%s)", event_name)


def reap_expired_external_access(db) -> Dict[str, int]:
    """Soft-disable external access whose end date has passed:
    1) active external grants past expires_at -> is_active=False +
       revoked_at + revoke_reason (the grants keep their row);
    2) external ACTIVE users past external_access_expires_at with no
       remaining active external grant -> status DEACTIVATED.
    Audited. Returns counts. Safe to run repeatedly (idempotent)."""
    now = _now()
    expired_grants = (
        db.query(models.ResourcePermission)
        .filter(models.ResourcePermission.is_external_user.is_(True))
        .filter(models.ResourcePermission.is_active.is_(True))
        .filter(models.ResourcePermission.revoked_at.is_(None))
        .filter(models.ResourcePermission.expires_at.isnot(None))
        .filter(models.ResourcePermission.expires_at <= now)
        .all()
    )
    for g in expired_grants:
        g.is_active = False
        g.revoked_at = now
        g.revoke_reason = "fx110_external_expiry"
        db.add(g)
    if expired_grants:
        by_tenant: Dict[int, list] = {}
        for g in expired_grants:
            by_tenant.setdefault(g.tenant_id, []).append(g.id)
        for tenant_id, grant_ids in by_tenant.items():
            _audit(db, tenant_id, "EXTERNAL_ACCESS_EXPIRED",
                   {"grants_disabled": grant_ids, "reason": "expiry"},
                   user_id=None)
        db.commit()

    active_user_ids = {
        row[0] for row in
        db.query(models.ResourcePermission.user_id)
        .filter(models.ResourcePermission.is_external_user.is_(True))
        .filter(models.ResourcePermission.is_active.is_(True))
        .filter(models.ResourcePermission.revoked_at.is_(None))
        .all()
        if row[0] is not None
    }

    expired_users = (
        db.query(models.User)
        .filter(models.User.is_external.is_(True))
        .filter(models.User.deleted_at.is_(None))
        .filter(models.User.status == models.UserStatus.ACTIVE)
        .filter(models.User.external_access_expires_at.isnot(None))
        .filter(models.User.external_access_expires_at <= now)
        .all()
    )
    deactivated = []
    for u in expired_users:
        if u.id in active_user_ids:
            continue  # still holds live external grants (e.g. re-invited)
        u.status = models.UserStatus.DEACTIVATED
        db.add(u)
        deactivated.append(u.id)
        _audit(db, u.tenant_id, "EXTERNAL_USER_EXPIRED",
               {"user_id": u.id, "reason": "expiry"}, user_id=u.id)
    if deactivated:
        db.commit()
    return {"grants_disabled": len(expired_grants),
            "users_deactivated": len(deactivated)}


def remind_inviters_about_expiry(db) -> int:
    """Remind the inviter N days before an external account expires
    (external_collab.reminder_days_before, default 7). Deduped:
    external_expiry_reminded_at is stamped on the external user, so
    each expiry is reminded exactly once."""
    now = _now()
    reminded = 0
    for u in (
        db.query(models.User)
        .filter(models.User.is_external.is_(True))
        .filter(models.User.deleted_at.is_(None))
        .filter(models.User.external_access_expires_at.isnot(None))
        .filter(models.User.external_access_expires_at > now)
        .filter(models.User.external_expiry_reminded_at.is_(None))
        .filter(models.User.invited_by_user_id.isnot(None))
        .all()
    ):
        window = timedelta(days=get_policy(
            db, u.tenant_id)["reminder_days_before"])
        if _aware(u.external_access_expires_at) - now <= window:
            inviter = (
                db.query(models.User)
                .filter(models.User.id == u.invited_by_user_id).first()
            )
            if inviter is not None:
                try:
                    db.add(models.Notification(
                        recipient_user_id=inviter.id,
                        notification_type="external_expiry_reminder",
                        message=(f"External collaborator access for "
                                 f"{u.username or u.email} expires on "
                                 f"{_aware(u.external_access_expires_at).date().isoformat()}. "
                                 f"Re-invite to extend access."),
                    ))
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    logger.warning("FX.110: reminder notify failed: %s", exc)
            u.external_expiry_reminded_at = now
            db.add(u)
            _audit(db, u.tenant_id, "EXTERNAL_EXPIRY_REMINDER",
                   {"external_user_id": u.id,
                    "invited_by_user_id": u.invited_by_user_id,
                    "expires_at": _aware(u.external_access_expires_at).isoformat()})
            reminded += 1
    if reminded:
        db.commit()
    return reminded


def run_external_expiry_tick(db) -> Dict[str, int]:
    """Entry point for the daily scheduler job (services/scheduler.py
    FX.110 block). Reminders first, then the reap — order is cosmetic,
    both are idempotent."""
    out = {"reminders": remind_inviters_about_expiry(db),
           "reap": reap_expired_external_access(db)}
    return out


# ---------------------------------------------------------------------------
# Tenant-admin operations
# ---------------------------------------------------------------------------

def list_external_collaborators(db, tenant_id: int) -> List[Dict[str, Any]]:
    """FX.110: external users listed SEPARATELY (their own admin page +
    the external bucket of access-review campaign payloads), with their
    active grants + end dates + restriction options."""
    users = (
        db.query(models.User)
        .filter(models.User.tenant_id == tenant_id)
        .filter(models.User.is_external.is_(True))
        .filter(models.User.deleted_at.is_(None))
        .order_by(models.User.id)
        .all()
    )
    rows = []
    for u in users:
        grants = [
            g for g in db.query(models.ResourcePermission)
            .filter(models.ResourcePermission.tenant_id == tenant_id)
            .filter(models.ResourcePermission.user_id == u.id)
            .filter(models.ResourcePermission.is_external_user.is_(True))
            .filter(models.ResourcePermission.is_active.is_(True))
            .filter(models.ResourcePermission.revoked_at.is_(None))
            .all()
        ]
        rows.append({
            "user_id": u.id,
            "username": u.username,
            "email": u.email,
            "status": u.status.value if u.status else None,
            "invited_by_user_id": u.invited_by_user_id,
            "external_access_expires_at": _aware(u.external_access_expires_at),
            "external_terms_accepted_at": _aware(u.external_terms_accepted_at),
            "external_profile_approved_by_user_id":
                u.external_profile_approved_by_user_id,
            "grants": [{
                "grant_id": g.id,
                "resource_type": g.resource_type.value
                if hasattr(g.resource_type, "value") else str(g.resource_type),
                "resource_id": g.resource_id,
                "permission_level": g.permission_level.value
                if g.permission_level else None,
                "expires_at": _aware(g.expires_at),
                "restriction_options": g.external_restriction_options or {},
            } for g in grants],
        })
    return rows


# CE-NOPAID-4.9c fold (MOVE-OUTSIDE-COLLABORATORS-2-FUNCS-TO-PAID-ADDON):
# the two access-review campaign helpers that used to sit here now live in
# the paid add-on package (CORE/API/paid_addon/outside_collab.py) next to the
# compliance model classes they read. They touched ONLY tables the add-on
# owns, and their only callers are paid-side (the admin router is denied in
# the CE carve; the test file sits under the wholesale tests deny), so this
# shared service keeps neither the code nor a reference to those tables.
# Paid callers import them from the add-on; behaviour is unchanged.


def remove_all_external_access(db, tenant_id: int,
                               actor_user_id: Optional[int]) -> Dict[str, int]:
    """Tenant-admin 'remove all external access' — SOFT and audited:
    revokes every active external grant (is_active=False + revoked_at +
    revoked_by + reason) and deactivates every ACTIVE external user.
    Nothing is hard-deleted (ISO 27001)."""
    now = _now()
    grants = (
        db.query(models.ResourcePermission)
        .filter(models.ResourcePermission.tenant_id == tenant_id)
        .filter(models.ResourcePermission.is_external_user.is_(True))
        .filter(models.ResourcePermission.is_active.is_(True))
        .filter(models.ResourcePermission.revoked_at.is_(None))
        .all()
    )
    grant_ids = []
    for g in grants:
        g.is_active = False
        g.revoked_at = now
        g.revoked_by_user_id = actor_user_id
        g.revoke_reason = "fx110_tenant_admin_remove_all"
        db.add(g)
        grant_ids.append(g.id)
    users = (
        db.query(models.User)
        .filter(models.User.tenant_id == tenant_id)
        .filter(models.User.is_external.is_(True))
        .filter(models.User.deleted_at.is_(None))
        .filter(models.User.status == models.UserStatus.ACTIVE)
        .all()
    )
    user_ids = []
    for u in users:
        u.status = models.UserStatus.DEACTIVATED
        db.add(u)
        user_ids.append(u.id)
    db.commit()
    _audit(db, tenant_id, "EXTERNAL_ACCESS_REMOVED_ALL",
           {"grants_revoked": grant_ids, "users_deactivated": user_ids,
            "actor_user_id": actor_user_id},
           user_id=actor_user_id)
    return {"grants_revoked": len(grant_ids),
            "users_deactivated": len(user_ids)}


def approve_external_user(db, tenant_id: int, user_id: int,
                          actor_user_id: Optional[int]) -> Dict[str, Any]:
    """Gov/DoD per-user approval: stamp the approving tenant admin on
    the external user so the first-access gate passes. Audited."""
    u = (
        db.query(models.User)
        .filter(models.User.id == user_id)
        .filter(models.User.tenant_id == tenant_id)
        .filter(models.User.is_external.is_(True))
        .filter(models.User.deleted_at.is_(None))
        .first()
    )
    if u is None:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="External user not found in this tenant.")
    u.external_profile_approved_by_user_id = actor_user_id
    db.add(u)
    db.commit()
    db.refresh(u)
    _audit(db, tenant_id, "EXTERNAL_USER_APPROVED",
           {"external_user_id": user_id, "approved_by": actor_user_id},
           user_id=actor_user_id)
    return {"user_id": user_id,
            "approved": True,
            "approved_by_user_id": actor_user_id,
            "approved_at": _now()}
