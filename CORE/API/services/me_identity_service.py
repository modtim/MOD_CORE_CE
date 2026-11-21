"""Service layer for the ``/me`` identity-tenancy domain (Gate 2b).

Business logic / policy for: profile + email-preference updates, RBAC "god
mode" resolution ("/me/permissions"), the 16-section GDPR data export
(Art. 20) with per-section failure isolation, account deactivation + PII
anonymization + API-key revocation (Art. 17), and owner-scoped session
listing/revocation.

Owns the transaction boundary; delegates ALL data access to
``crud/me_identity.py``; carries no HTTP concerns (raises ``ServiceError``
subclasses that the router maps to ``HTTPException``, preserving the existing
response codes/bodies exactly).

Behaviour-preserving Gate-2 migration: every method reproduces the previous
inline-router behaviour, just relocated.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy.orm import Session

import models
from crud import me_identity as me_identity_crud
from services._base import ConflictError, NotFoundError

logger = logging.getLogger(__name__)


def _iso(dt):
    """Safely serialize a datetime (or None) to an ISO-8601 string."""
    return dt.isoformat() if dt else None


def _enum(val):
    """Safely serialize an enum (or None/plain value) to its .value."""
    return val.value if hasattr(val, "value") else val


class MeIdentityService:
    """Policy + transaction boundary for the ``/me`` identity-tenancy domain."""

    # -- Profile / preferences ------------------------------------------------

    def update_profile(self, db: Session, user: "models.User", data) -> Dict[str, Any]:
        """Update the current user's profile (name fields)."""
        if data.first_name is not None:
            user.first_name = data.first_name
        if data.last_name is not None:
            user.last_name = data.last_name
        db.commit()
        return {
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "username": user.username,
        }

    def get_email_preferences(self, user: "models.User") -> Dict[str, Any]:
        """Read the current user's lifecycle-email opt-out preference."""
        return {
            "lifecycle_emails_opted_out": bool(
                getattr(user, "lifecycle_emails_opted_out", False)
            )
        }

    def set_email_preferences(self, db: Session, user: "models.User", data) -> Dict[str, Any]:
        """Update the current user's lifecycle-email opt-out preference."""
        user.lifecycle_emails_opted_out = bool(data.lifecycle_emails_opted_out)
        db.commit()
        return {"lifecycle_emails_opted_out": user.lifecycle_emails_opted_out}

    # -- Notification preferences (WS4.Q22) --------------------------------

    def get_notification_preferences(self, db: Session, user: "models.User") -> Dict[str, Any]:
        """Read the current user's per-category, per-channel notification
        preferences. Categories/channels with no stored row default to
        enabled (True)."""
        from crud import notification_preferences as notification_prefs_crud
        return notification_prefs_crud.get_preferences_map(db, user.id)

    def set_notification_preferences(self, db: Session, user: "models.User", data) -> Dict[str, Any]:
        """Upsert the current user's notification preferences (partial
        update — only categories/channels present in the payload change)."""
        from crud import notification_preferences as notification_prefs_crud
        # .dict(exclude_unset=True) is recursive in pydantic v2 — nested
        # NotificationChannelPreference fields the caller didn't set are
        # dropped too, so `updates[category]` only carries channels the
        # request actually specified.
        updates = {
            category: channels
            for category, channels in data.dict(exclude_unset=True).items()
            if channels
        }
        return notification_prefs_crud.set_preferences(db, user.id, updates)

    # -- Permissions ------------------------------------------------------

    @staticmethod
    def _capability_dict(cap: "models.Capability") -> Dict[str, Any]:
        return {
            "id": cap.id,
            "name": f"{cap.resource_type.value}:{cap.name}",
            "description": cap.description,
            "display_name": cap.display_name,
            "resource_type": cap.resource_type.value,
            "capability_name": cap.name,
        }

    def get_my_permissions(self, db: Session, user: "models.User") -> List[Dict[str, Any]]:
        """The caller's capabilities. Platform admins get every capability
        ("god mode"); otherwise the capabilities of their custom role, if any."""
        if user.is_platform_admin:
            return [self._capability_dict(cap) for cap in me_identity_crud.list_all_capabilities(db)]

        if not user.custom_role_id:
            return []

        role = me_identity_crud.get_role_with_capabilities(db, user.custom_role_id)
        if not role:
            return []

        return [self._capability_dict(cap) for cap in role.capabilities]

    # -- GDPR data export ("/me/data-export") --------------------------------

    def export_personal_data(self, db: Session, user: "models.User") -> Dict[str, Any]:
        """Export ALL data associated with the current user (GDPR Art. 20).

        A comprehensive export spans ~16 tables. One table's schema drift or a
        transient error must not deny the user their entire export, so each
        section runs in isolation: on failure the session is rolled back
        (Postgres aborts the whole transaction after any error), the section
        name is recorded in ``unavailable_sections``, and an empty default is
        used instead.
        """
        uid = user.id
        tid = user.tenant_id
        unavailable_sections: List[str] = []

        def _section(name, fn, default):
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 — export must never 500 on one table
                logger.warning("data-export: section %r failed for user %s: %s", name, uid, e)
                try:
                    db.rollback()
                except Exception:
                    pass
                unavailable_sections.append(name)
                return default

        # -- 1. Identity / profile ---------------------------------------
        profile = {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "status": _enum(user.status),
            "tenant_id": user.tenant_id,
            "is_platform_admin": user.is_platform_admin,
            "platform_role": _enum(getattr(user, "platform_role", None)),
            "is_external": getattr(user, "is_external", None),
            "invited_by_user_id": getattr(user, "invited_by_user_id", None),
            "parent_user_id": getattr(user, "parent_user_id", None),
            "custom_role_id": getattr(user, "custom_role_id", None),
            "keycloak_user_id": getattr(user, "keycloak_user_id", None),
            "onboarding_complete": getattr(user, "onboarding_complete", None),
            "lifecycle_emails_opted_out": getattr(user, "lifecycle_emails_opted_out", None),
            "is_email_verified": getattr(user, "is_email_verified", None),
            "email_verified_at": _iso(getattr(user, "email_verified_at", None)),
            "provisioning_source": getattr(user, "provisioning_source", None),
            "external_id": getattr(user, "external_id", None),
            "storage_attributed_bytes": getattr(user, "storage_attributed_bytes", None),
            "created_at": _iso(user.created_at),
            "updated_at": _iso(getattr(user, "updated_at", None)),
            "deleted_at": _iso(getattr(user, "deleted_at", None)),
            # GY.V5 — the ``minor`` data label (COPPA). Part of the DSAR
            # export so the subject (and their guardian) can see it.
            "is_minor": bool(getattr(user, "is_minor", False)),
        }

        # -- 2. Permission changes (audit of grants/revokes/requests) --------
        perm_audit = _section(
            "permission_changes",
            lambda: me_identity_crud.get_permission_audit_for_user(db, tid, uid),
            [],
        )
        permission_changes = [{
            "id": p.id,
            "action": _enum(p.action),
            "actor_user_id": p.actor_user_id,
            "target_user_id": p.target_user_id,
            "role": "actor" if p.actor_user_id == uid else "target",
            "resource_type": p.resource_type,
            "resource_id": p.resource_id,
            "permission_level": p.permission_level,
            "metadata": p.audit_metadata,
            "ip_address": p.ip_address,
            "user_agent": p.user_agent,
            "timestamp": _iso(p.timestamp),
        } for p in perm_audit]

        # -- 3. Permission grants held by / issued by the user ---------------
        grants = _section(
            "permission_grants",
            lambda: me_identity_crud.get_permission_grants_for_user(db, tid, uid),
            [],
        )
        permission_grants = [{
            "id": g.id,
            "relationship": "grantee" if g.user_id == uid else "granted_by_me",
            "resource_type": _enum(g.resource_type),
            "resource_id": g.resource_id,
            "capability_id": g.capability_id,
            "permission_level": _enum(g.permission_level),
            "granted_by_user_id": g.granted_by_user_id,
            "granted_at": _iso(g.granted_at),
            "expires_at": _iso(g.expires_at),
            "is_active": g.is_active,
            "revoked_at": _iso(g.revoked_at),
        } for g in grants]

        # Workflow shares involving the user (resource-scoped grants on workflows)
        workflow_shares = [g for g in permission_grants if g["resource_type"] == "workflow"]

        # -- 4. Permission requests the user filed ----------------------------
        p_requests = _section(
            "permission_requests",
            lambda: me_identity_crud.get_permission_requests_for_user(db, tid, uid),
            [],
        )
        permission_requests = [{
            "id": r.id,
            "resource_type": r.resource_type,
            "resource_id": r.resource_id,
            "cog_id": r.cog_id,
            "requested_level": _enum(r.requested_level),
            "requested_duration_days": r.requested_duration_days,
            "justification": r.justification,
            "status": _enum(r.status),
            "reviewed_by_user_id": r.reviewed_by_user_id,
            "reviewed_at": _iso(r.reviewed_at),
            "review_notes": r.review_notes,
            "created_at": _iso(r.created_at),
        } for r in p_requests]

        # -- 5. Workflows owned by the user -----------------------------------
        workflows = _section(
            "workflows",
            lambda: me_identity_crud.get_workflows_owned(db, tid, uid),
            [],
        )
        wf_data = [{
            "id": w.id,
            "name": w.name,
            "is_system": w.is_system,
            "created_by_user_id": w.created_by_user_id,
        } for w in workflows]

        # -- 6. Files / cogs owned by the user ---------------------------------
        cogs = _section(
            "files",
            lambda: me_identity_crud.get_cogs_owned(db, tid, uid),
            [],
        )
        files = [{
            "id": c.id,
            "name": c.name,
            "type": _enum(c.cog_type),
            "parent_id": c.parent_id,
            "data_classification": getattr(c, "data_classification", None),
            "created_at": _iso(c.created_at),
            "tags": [t.name for t in getattr(c, "tags", [])],
        } for c in cogs]

        # -- 7. Tags applied to the user's cogs --------------------------------
        tag_names = sorted({t.name for c in cogs for t in getattr(c, "tags", [])})
        tags = [{"name": n} for n in tag_names]

        # -- 8. API keys (metadata only — hashes/secrets redacted) -------------
        api_keys = _section(
            "api_keys",
            lambda: me_identity_crud.get_active_api_keys(db, uid),
            [],
        )
        keys = [{
            "key_prefix": k.key_prefix,
            "description": k.description,
            "is_read_only": getattr(k, "is_read_only", None),
            "expires_at": _iso(k.expires_at),
            "last_used_at": _iso(k.last_used_at),
            "created_at": _iso(k.created_at),
            # NOTE: hashed_key / encrypted_keycloak_secret intentionally omitted.
        } for k in api_keys]

        # -- 9. Secrets (names/references only — values redacted) --------------
        secret_rows = _section(
            "secrets",
            lambda: me_identity_crud.get_secrets_owned(db, tid, uid),
            [],
        )
        secrets = [{
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "created_at": _iso(s.created_at),
            # NOTE: encrypted_value intentionally omitted.
        } for s in secret_rows]

        # -- 10. Invitations sent / received ------------------------------------
        sent_invites = _section(
            "invitations_sent",
            lambda: me_identity_crud.get_invitations_sent(db, tid, uid),
            [],
        )
        invitations_sent = [{
            "id": i.id,
            "email": i.email,
            "status": _enum(i.status),
            "expires_at": _iso(i.expires_at),
            "accepted_at": _iso(i.accepted_at),
            "created_at": _iso(i.created_at),
            # NOTE: invitation token intentionally omitted.
        } for i in sent_invites]

        received_invites = _section(
            "invitations_received",
            lambda: me_identity_crud.get_invitations_received(db, user.email),
            [],
        )
        invitations_received = [{
            "id": i.id,
            "tenant_id": i.tenant_id,
            "invited_by_user_id": i.invited_by_user_id,
            "status": _enum(i.status),
            "expires_at": _iso(i.expires_at),
            "accepted_at": _iso(i.accepted_at),
            "created_at": _iso(i.created_at),
        } for i in received_invites]

        # -- 11. Subscription (tenant-scoped) -----------------------------------
        sub = _section(
            "subscription",
            lambda: me_identity_crud.get_subscription_for_tenant(db, tid),
            None,
        )
        subscription = None
        if sub:
            subscription = {
                "id": sub.id,
                "plan_id": sub.plan_id,
                "status": sub.status,
                "is_trial": getattr(sub, "is_trial", None),
                "trial_ends_at": _iso(sub.trial_ends_at),
                "current_period_start": _iso(sub.current_period_start),
                "current_period_ends_at": _iso(sub.current_period_ends_at),
                "allow_overage": getattr(sub, "allow_overage", None),
                "pending_plan_id": getattr(sub, "pending_plan_id", None),
                "created_at": _iso(sub.created_at),
                # NOTE: stripe_subscription_id / stripe_customer_id intentionally omitted.
            }

        # -- 12. Credit ledger / history (attributed to the user) ---------------
        ledger = _section(
            "credit_history",
            lambda: me_identity_crud.get_credit_ledger_for_user(db, tid, uid),
            [],
        )
        credits = [{
            "type": _enum(e.entry_type),
            "amount": e.amount_in_credits,
            "balance_after": e.balance_after,
            "workflow_run_id": e.workflow_run_id,
            "notes": e.notes,
            "created_at": _iso(e.created_at),
        } for e in ledger]

        # -- 13. Notifications -----------------------------------------------
        notifs = _section(
            "notifications",
            lambda: me_identity_crud.get_notifications_for_user(db, uid),
            [],
        )
        notifications = [{
            "id": n.id,
            "message": n.message,
            "notification_type": n.notification_type,
            "is_read": n.is_read,
            "is_archived": getattr(n, "is_archived", None),
            "link_url": n.link_url,
            "created_at": _iso(n.created_at),
        } for n in notifs]

        # -- 14. Team memberships -----------------------------------------------
        team_memberships = _section("team_memberships", lambda: [{
            "id": t.id,
            "name": t.name,
            "tenant_id": t.tenant_id,
        } for t in getattr(user, "teams", [])], [])

        # -- 15. Sessions -------------------------------------------------------
        sessions = _section(
            "sessions",
            lambda: me_identity_crud.get_sessions_for_user(db, uid),
            [],
        )
        session_list = [{
            "id": s.id,
            "device_info": s.device_info,
            "ip_address": s.ip_address,
            "last_active_at": _iso(s.last_active_at),
            "created_at": _iso(s.created_at),
        } for s in sessions]

        # -- 16. Login / audit events ---------------------------------------
        audit = _section(
            "audit_events",
            lambda: me_identity_crud.get_audit_events_for_user(db, uid),
            [],
        )
        audit_events = [{
            "id": a.id,
            "event_name": a.event_name,
            "details": a.details,
            "timestamp": _iso(a.timestamp),
        } for a in audit]

        # -- 17. Consent records (GY.V5) ------------------------------------
        from services import consent
        consent_rows = _section(
            "consent_records",
            lambda: consent.consent_history(db, tenant_id=tid,
                                            subject_user_id=uid),
            [],
        )

        # -- 18. Enterprise leads (GSEC R-119) --------------------------------
        enterprise_leads_rows = _section(
            "enterprise_leads",
            lambda: me_identity_crud.get_enterprise_leads_for_user(db, user.email),
            [],
        )
        enterprise_leads = [{
            "id": l.id,
            "name": l.name,
            "email": l.email,
            "company": l.company,
            "message": l.message,
            "status": l.status,
            "pii_scrubbed_at": _iso(l.pii_scrubbed_at),
            "created_at": _iso(l.created_at),
        } for l in enterprise_leads_rows]

        return {
            "export_version": "2.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "user_id": uid,
            "tenant_id": tid,
            "user": profile,
            "permission_changes": permission_changes,
            "permission_grants": permission_grants,
            "workflow_shares": workflow_shares,
            "permission_requests": permission_requests,
            "workflows": wf_data,
            "files": files,
            "tags": tags,
            "api_keys": keys,
            "secrets": secrets,
            "invitations_sent": invitations_sent,
            "invitations_received": invitations_received,
            "subscription": subscription,
            "credit_history": credits,
            "notifications": notifications,
            "team_memberships": team_memberships,
            "sessions": session_list,
            "audit_events": audit_events,
            "consent_records": consent_rows,
            "enterprise_leads": enterprise_leads,
            # Sections that could not be gathered (e.g. transient DB error or a
            # pending schema migration). Empty on a healthy export.
            "unavailable_sections": unavailable_sections,
        }

    # -- Account deletion ("/me/account", GDPR Art. 17) ------------------

    def request_account_deletion(self, db: Session, user: "models.User") -> Dict[str, Any]:
        """Request deletion of the current user's account.

        The account is immediately deactivated. Billing records are
        anonymized but retained for legal/tax compliance (typically 7 years).
        Raises ``ConflictError`` (router maps -> 409) if any of the tenant's
        outputs are under legal hold (#113).
        """
        held_outputs = me_identity_crud.count_legal_hold_outputs(db, user.tenant_id)
        if held_outputs > 0:
            raise ConflictError(
                f"Cannot delete account: {held_outputs} outputs are under legal hold. "
                "Remove legal holds first."
            )

        # Deactivate user immediately — cooling period (privacy.erasure_grace_days,
        # default 30) before the privacy-erasure sweep destroys the user's
        # object bytes (GSEC R-016 / FX.15). The sweep never deletes rows
        # (ISO 27001) — bytes + PII go, the row stays for audit FKs.
        from services import platform_settings as _psr
        grace_days = int(_psr.get_setting("privacy.erasure_grace_days") or 30)
        user.status = models.UserStatus.DEACTIVATED
        # Anchor the erasure grace window so the scheduler sweep knows
        # when the cooling period ends.
        user.deletion_requested_at = datetime.now(timezone.utc)
        # Anonymize PII immediately per GDPR
        user.email = f"deleted-{user.id}@deleted.local"
        user.first_name = None
        user.last_name = None

        # Revoke all API keys (soft-delete + audit row per key).
        from crud.user_cud import revoke_user_api_keys
        revoke_user_api_keys(db, user.id, reason="account_deletion")

        # Audit: account deletion requested
        try:
            db.add(models.AuditEvent(
                tenant_id=user.tenant_id, user_id=user.id,
                event_name="ACCOUNT_DELETION_REQUESTED",
                details={"cooling_period_days": grace_days},
            ))
        except Exception:
            pass

        db.commit()

        logger.info(f"Account deletion requested: user_id={user.id}, tenant_id={user.tenant_id}")

        return {
            "status": "account_deactivated",
            "message": "Your account has been deactivated. Your files and encryption keys will be destroyed after the grace period "
                       f"({grace_days} days) and your personal details anonymized. A minimal anonymized record is kept for legal and audit purposes.",
            "user_id": user.id,
            "permanent_deletion_after_days": grace_days,
        }

    # -- Session management ("/me/sessions") -------------------------------

    def list_sessions(self, db: Session, user: "models.User") -> List[Dict[str, Any]]:
        """List active sessions for the current user.

        FX.112 PART 1: rows carry ``keycloak_session_id`` (the Keycloak
        session state this local row was minted from) so the UI can
        identify which KC session a revoke targets; revocation
        propagates to Keycloak via the admin sessions API
        (services/me_auth_apikeys_service.revoke_session)."""
        sessions = me_identity_crud.get_sessions_for_user(db, user.id)
        return [
            {
                "id": s.id,
                "device_info": s.device_info,
                "ip_address": s.ip_address,
                "last_active_at": s.last_active_at.isoformat() if s.last_active_at else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "keycloak_session_id": s.keycloak_session_id,
            }
            for s in sessions
        ]

    def revoke_session(self, db: Session, user: "models.User", session_id: int) -> None:
        """Revoke a specific session. Raises ``NotFoundError`` (router maps ->
        404) if it doesn't exist or isn't the caller's.

        FX.112 PART 1: delegates to ``me_auth_apikeys_service.revoke_session``
        so the sign-out is audited (SESSION_SIGN_OUT_SINGLE) and propagated
        to Keycloak (best-effort)."""
        from services import me_auth_apikeys_service
        me_auth_apikeys_service.revoke_session(db, user, session_id)


me_identity_service = MeIdentityService()
