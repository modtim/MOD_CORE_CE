"""Business logic for the `routers/invitations.py` domain (Gate 2b migration).

Moved out of `routers/invitations.py` so the router stays HTTP-only (parse
input, call here, shape the response). Data access goes through
`crud.invitations` (this domain's new query-only module, replacing the 5
inline `db.query(...)` call sites that used to live in the router) and the
existing `crud/invitation_cud.py` functions (`create_invitation`,
`get_invitation_by_id`, `delete_invitation`, `get_pending_invitations_for_tenant`,
`get_pending_invitation_by_token`, all re-exported off the top-level `crud`
package) plus other existing top-level `crud` helpers already used by this
domain (`get_email_config`, `get_secret_by_name`,
`get_decrypted_secret_by_name`, `count_active_users_in_tenant`).

Error handling: this module raises `HTTPException` directly, matching the
sibling `services/external_invitations_service.py` (same domain family) —
the pre-migration handlers here use unusually varied status codes (400, 403,
404, 409) with messages that must stay byte-identical (some are load-bearing
for a security/audit fix — see the no-cross-tenant-existence-oracle comments
below), so raising the exact original `HTTPException` inline keeps behavior
byte-identical rather than laundering it through the `ServiceError` pair in
`services/_base.py`.

Deliberately NOT changed here (flagged, not fixed — outside this migration's
scope):
  * `cancel_invitation` still calls `crud.delete_invitation`, which does a
    hard `db.delete()` on the invitation row rather than a soft delete, even
    though `Invitation` has `deleted_at` and every read in this domain
    already excludes soft-deleted rows (ISO 27001 no-hard-delete — MEMORY
    iso-no-hard-delete). Pre-existing behavior in `crud/invitation_cud.py`,
    preserved verbatim; not touched by this migration (that module is shared
    with `external_invitations_service.py` and others).
  * `list_pending_invitations` accepts a `tenant_id` parameter but ignores it
    for everyone (always scopes to `current_user.tenant_id`) — pre-existing,
    intentional per the inline comment ("Tenant-scoped for everyone (ISO/DoD
    tenant isolation): no cross-tenant view."), preserved verbatim.

No JSONB `.astext` usage in this domain's queries — nothing to fix per the
migration's JSONB rule.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import crud
import models
import schemas
from crud import invitations as crud_inv
from utils.http_errors import internal_http_error
from utils.smtp_security import config_is_complete


def _ce_edition_no_cap() -> bool:
    """True on the CE edition (no seat cap); False on paid or when the
    edition probe errors (fail to PAID behaviour)."""
    try:
        from utils.paid_import import is_ce_edition
        return bool(is_ce_edition())
    except Exception:  # noqa: BLE001 - fail to the PAID path
        return False

_PLATFORM_TENANT_ID = 1


# =============================================================================
# POST /invitations/
# =============================================================================


def create_and_send_invitation(
    db: Session,
    invitation_data: schemas.InvitationCreate,
    background_tasks: BackgroundTasks,
    current_user: models.User,
) -> schemas.InvitationCreated:
    """Create an invitation for a new user to join a tenant and queue the
    invitation email as a background task. Body of
    `create_and_send_invitation` (routers/invitations.py)."""
    # Determine target tenant ID
    # Platform admins can specify tenant_id, others use their own tenant
    if invitation_data.tenant_id is not None:
        if not current_user.is_platform_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only platform administrators can invite users to other tenants.",
            )
        # CE carve: foreign tenant_id invite denied (open-core/manifest.yaml:1708 denies tenants.py in CE; CE serves only tenants_ce.py:1-87 read-only). A CE platform admin must not invite to another tenant.
        from utils.paid_import import is_ce_edition
        if is_ce_edition() and invitation_data.tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only platform administrators can invite users to other tenants.",
            )
        target_tenant_id = invitation_data.tenant_id

        # Verify target tenant exists
        target_tenant = crud_inv.get_tenant_by_id(db, target_tenant_id)
        if not target_tenant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tenant with ID {target_tenant_id} not found.",
            )
    else:
        target_tenant_id = current_user.tenant_id

    # ========================================================================
    # SEAT LIMIT CHECK - Ensure tenant has available seats
    # ========================================================================
    from utils.usage_enforcement import check_seat_limit, _is_saas
    from utils.paid_import import is_ce_edition

    # Only enforce for non-platform-admin invites
    if not current_user.is_platform_admin:
        check_seat_limit(db=db, tenant_id=target_tenant_id, raise_on_limit=True)
        # FX.41 (Q23=A) — tenant strictly OVER its (downgraded) seat limit:
        # block the NEW invite with a clear "what's over and by how much".
        from utils.paid_import import optional_paid
        _downgrade_mod = optional_paid("services.downgrade_readonly_service")
        if _downgrade_mod is not None:
            try:
                _downgrade_mod.block_if_overage(db, target_tenant_id, "seats")
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001
                import logging as _logging
                _logging.getLogger(__name__).exception(
                    "overage seat gate failed (proceeding)")

    # 1. Check if a user with this email already exists.
    #
    # Two cases must respond differently:
    #
    #   (a) Email belongs to a user IN THIS tenant — surface a
    #       helpful "already on your team" message. No information
    #       leak; the admin already sees this user on the Users page.
    #
    #   (b) Email belongs to a user in ANOTHER tenant — the admin
    #       must NOT learn that. Returning "user already exists"
    #       lets any tenant admin probe whether arbitrary emails
    #       are registered anywhere on the platform (cross-tenant
    #       existence oracle; flagged by the 2026-06-04 audit).
    #       Platform admins are exempt because they need cross-
    #       tenant visibility for support work.
    existing_user = crud_inv.get_user_by_email(db, invitation_data.email)
    if existing_user:
        if existing_user.tenant_id == target_tenant_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this email address is already a member of this tenant.",
            )
        if current_user.is_platform_admin:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A user with this email address already exists in tenant {existing_user.tenant_id}.",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to send an invitation to this email address. If you believe this is in error, please contact support.",
        )

    # 2. Check if a pending invitation already exists for this email in this tenant
    existing_invitation = crud_inv.get_pending_invitation_by_tenant_and_email(
        db, target_tenant_id, invitation_data.email
    )
    if existing_invitation:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A pending invitation for {invitation_data.email} already exists. Please cancel the existing invitation first or wait for it to expire.",
        )

    # 3. SaaS-only redundant seat/subscription gate.
    # On-prem, seat capacity is governed SOLELY by check_seat_limit() above
    # (resolver-aware; unlimited on the Core baseline). This direct
    # plan.max_seats read is not resolver-aware and would wrongly 403 an
    # on-prem tenant on Free's max_seats=1 even after check_seat_limit passed,
    # so it is gated to SaaS. On SaaS it also denies invites when the tenant
    # has no subscription (a check check_seat_limit does not make).
    # Platform tenant (ID=1) has unlimited seats for platform admins.
    # CE has NO seat quantity cap (owner rule): the whole plan/subscription
    # gate is skipped on CE (plan seat number ignored incl. 0, and a missing
    # subscription row does not block). The edition probe fails to PAID.
    if (_is_saas() and not _ce_edition_no_cap()
            and not (current_user.is_platform_admin and target_tenant_id == 1)):
        # Get the target tenant's subscription and plan
        target_tenant = crud_inv.get_tenant_by_id(db, target_tenant_id)

        if target_tenant and target_tenant.subscription and target_tenant.subscription.plan:
            plan = target_tenant.subscription.plan
            max_seats = plan.max_seats

            # Count current active users in the tenant
            current_seats_used = crud.count_active_users_in_tenant(db, target_tenant_id)

            # 0 = blocked (soft-suspend). UNLIMITED_SENTINEL (1Q)
            # never trips at realistic seat counts.
            if current_seats_used >= max_seats:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Cannot invite new user. Your {plan.name} plan allows {max_seats} seat{'s' if max_seats != 1 else ''}, and you currently have {current_seats_used} active user{'s' if current_seats_used != 1 else ''}. Please remove inactive users to invite more team members."
                )
        elif not target_tenant or not target_tenant.subscription:
            # No subscription = no plan = cannot invite users (except platform admin to platform tenant)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot invite new users. Invitations are not available for this tenant."
            )

    # 4. Validate email configuration before creating invitation
    # For cross-tenant invitations by platform admin, use Platform tenant's email config
    # For same-tenant invitations, use the target tenant's email config
    email_tenant_id = 1 if (invitation_data.tenant_id is not None and current_user.is_platform_admin) else target_tenant_id
    email_config = crud.get_email_config(db, tenant_id=email_tenant_id)

    # On-prem platform-email fallback:
    # When NOT running in SaaS mode, a tenant that has not configured its own
    # email settings falls back to the PLATFORM ADMIN's / platform-level email
    # configuration (platform tenant, id 1) rather than erroring. A tenant-level
    # config always wins when present (checked first above). SaaS keeps strict
    # per-tenant isolation — no cross-tenant fallback there. This mirrors the
    # send path (crud.get_decrypted_email_config already resolves platform
    # config), so validation and the actual send agree on which config is used.
    if not email_config and email_tenant_id != _PLATFORM_TENANT_ID and not _is_saas():
        platform_email_config = crud.get_email_config(db, tenant_id=_PLATFORM_TENANT_ID)
        if platform_email_config:
            email_tenant_id = _PLATFORM_TENANT_ID
            email_config = platform_email_config

    # Build list of missing configuration items
    missing_config = []
    setup_instructions = []

    if not email_config:
        missing_config.append("Email configuration")
        setup_instructions.append(
            "1. Go to Settings > Email Configuration and fill in your SMTP server (any SMTP service works)\n"
            "2. Fill in Sender Name and Sender Email\n"
            "3. Click 'Save Configuration'"
        )
    else:
        # Validate based on provider
        provider = email_config.provider or "brevo"

        if not email_config.sender_name or not email_config.sender_email:
            missing_config.append("Sender information")
            setup_instructions.append(
                "1. Go to Settings → Email Configuration\n"
                "2. Fill in Sender Name (e.g., 'Your Company')\n"
                "3. Fill in Sender Email (e.g., 'noreply@yourcompany.com')\n"
                "4. Click 'Save Configuration'"
            )

        if provider == "brevo":
            # Check for Brevo secrets and validate they're not placeholders
            brevo_api_key = crud.get_secret_by_name(db, "brevo_api_key", email_tenant_id)
            brevo_template_id = crud.get_secret_by_name(db, "brevo_template_id", email_tenant_id)

            # Check API key
            if not brevo_api_key:
                missing_config.append("Brevo API Key")
                setup_instructions.append(
                    "1. Go to https://app.brevo.com/settings/keys/api to get your API key\n"
                    "2. Go to Settings → Secrets in your dashboard\n"
                    "3. Find brevo_api_key and click 'Configure'\n"
                    "4. Enter your actual Brevo API key\n"
                    "5. Click 'OK'"
                )
            else:
                # Verify it's not a placeholder value
                decrypted_key = crud.get_decrypted_secret_by_name(db, "brevo_api_key", email_tenant_id)
                if not decrypted_key or decrypted_key.strip() == '' or decrypted_key == 'PLEASE_CONFIGURE':
                    missing_config.append("Brevo API Key")
                    setup_instructions.append(
                        "1. Go to https://app.brevo.com/settings/keys/api to get your API key\n"
                        "2. Go to Settings → Secrets in your dashboard\n"
                        "3. Find brevo_api_key and click 'Configure'\n"
                        "4. Enter your actual Brevo API key\n"
                        "5. Click 'OK'"
                    )

            # Check template ID
            if not brevo_template_id:
                missing_config.append("Brevo Template ID")
                setup_instructions.append(
                    "1. Go to https://app.brevo.com and create an email template\n"
                    "2. Use these variables in your template:\n"
                    "   - {{params.RECIPIENT_NAME}} - Name of person being invited\n"
                    "   - {{params.SENDER_NAME}} - Your company name\n"
                    "   - {{params.INVITATION_LINK}} - Link to accept invitation\n"
                    "3. Note the Template ID (shown in the template list or URL)\n"
                    "4. Go to Settings → Secrets in your dashboard\n"
                    "5. Find brevo_template_id and click 'Configure'\n"
                    "6. Enter your template ID number (e.g., '1')\n"
                    "7. Click 'OK'"
                )
            else:
                # Verify it's not a placeholder value
                decrypted_template = crud.get_decrypted_secret_by_name(db, "brevo_template_id", email_tenant_id)
                if not decrypted_template or decrypted_template.strip() == '' or decrypted_template == 'PLEASE_CONFIGURE':
                    missing_config.append("Brevo Template ID")
                    setup_instructions.append(
                        "1. Go to https://app.brevo.com and create an email template\n"
                        "2. Use these variables in your template:\n"
                        "   - {{params.RECIPIENT_NAME}} - Name of person being invited\n"
                        "   - {{params.SENDER_NAME}} - Your company name\n"
                        "   - {{params.INVITATION_LINK}} - Link to accept invitation\n"
                        "3. Note the Template ID (shown in the template list or URL)\n"
                        "4. Go to Settings → Secrets in your dashboard\n"
                        "5. Find brevo_template_id and click 'Configure'\n"
                        "6. Enter your template ID number (e.g., '1')\n"
                        "7. Click 'OK'"
                    )

        elif provider == "mailgun":
            # Check for Mailgun secrets
            mailgun_api_key = crud.get_secret_by_name(db, "mailgun_api_key", email_tenant_id)
            mailgun_domain = crud.get_secret_by_name(db, "mailgun_domain", email_tenant_id)

            if not mailgun_api_key:
                missing_config.append("Mailgun API Key")
                setup_instructions.append(
                    "1. Go to Settings → Secrets\n"
                    "2. Find 'mailgun_api_key' and click 'Configure'\n"
                    "3. Enter your Mailgun API key\n"
                    "4. Click 'OK'"
                )
            else:
                decrypted_key = crud.get_decrypted_secret_by_name(db, "mailgun_api_key", email_tenant_id)
                if not decrypted_key or decrypted_key.strip() == '' or decrypted_key == 'PLEASE_CONFIGURE':
                    missing_config.append("Mailgun API Key")
                    setup_instructions.append(
                        "1. Go to Settings → Secrets\n"
                        "2. Find 'mailgun_api_key' and click 'Configure'\n"
                        "3. Enter your Mailgun API key\n"
                        "4. Click 'OK'"
                    )

            if not mailgun_domain:
                missing_config.append("Mailgun Domain")
                setup_instructions.append(
                    "1. Go to Settings → Secrets\n"
                    "2. Find 'mailgun_domain' and click 'Configure'\n"
                    "3. Enter your Mailgun domain (e.g., email.yourdomain.com)\n"
                    "4. Click 'OK'"
                )
            else:
                decrypted_domain = crud.get_decrypted_secret_by_name(db, "mailgun_domain", email_tenant_id)
                if not decrypted_domain or decrypted_domain.strip() == '' or decrypted_domain == 'PLEASE_CONFIGURE':
                    missing_config.append("Mailgun Domain")
                    setup_instructions.append(
                        "1. Go to Settings → Secrets\n"
                        "2. Find 'mailgun_domain' and click 'Configure'\n"
                        "3. Enter your Mailgun domain (e.g., email.yourdomain.com)\n"
                        "4. Click 'OK'"
                    )

        elif provider == "custom":
            # FIX-INVITE-EMAIL-NOAUTH-SMTP: use the SAME completeness rule as the
            # sender (`utils/smtp_security.config_is_complete`, the single rule
            # also used by services/admin_email_config_service.py:73 and
            # routers/admin_email_config.py:402). The old four-field test here
            # (host + port + user + password) rejected the Mailpit / local-relay
            # preset — host + sender + an EXPLICIT security choice, no user and
            # no password — so the invitation was created (201) while the send
            # was silently skipped, and `is_configured` on POST /v1/system/email
            # already said True for the very same row. A truly empty config
            # (no host, or no sender) still lands here: no send, log line kept.
            smtp_password_set = bool(
                email_config.encrypted_smtp_password
            ) or bool(getattr(email_config, "password_vault_path", None))
            if not config_is_complete(
                email_config.smtp_host,
                email_config.smtp_user,
                smtp_password_set,
                email_config.sender_email,
                getattr(email_config, "smtp_security", None),
            ):
                missing_config.append("SMTP server settings")
                setup_instructions.append(
                    "1. Go to Settings → Email Configuration\n"
                    "2. Select 'Use a custom SMTP server'\n"
                    "3. Fill in:\n"
                    "   - SMTP Host (e.g., smtp.gmail.com)\n"
                    "   - SMTP Port (e.g., 587)\n"
                    "   - SMTP Username\n"
                    "   - SMTP Password\n"
                    "4. Click 'Send Test Email' to verify\n"
                    "5. Click 'Save Configuration'"
                )

    # BUG-1027i: CE edition only — skip the missing-config 400 and the email queue.
    # Pro / on-prem / SaaS: unchanged.
    ce_no_mail = bool(missing_config) and is_ce_edition()

    if missing_config and not ce_no_mail:
        error_message = f"Cannot send invitations. Missing configuration: {', '.join(missing_config)}\n\n"
        error_message += "Please complete the following setup steps:\n\n"
        error_message += "\n\n".join(setup_instructions)

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_message
        )

    # 5. Create the invitation record in the database
    try:
        db_invitation = crud.create_invitation(db=db, invitation=invitation_data, inviter=current_user, target_tenant_id=target_tenant_id)
    except IntegrityError as e:
        db.rollback()
        # Handle duplicate invitation constraint violation
        if 'uq_tenant_pending_invitation' in str(e.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A pending invitation for {invitation_data.email} already exists in this tenant. Please cancel the existing invitation first or wait for it to expire.",
            )
        # Re-raise other integrity errors
        raise internal_http_error(status.HTTP_400_BAD_REQUEST, "Invitation could not be saved", e) from e

    # 6. Prepare task configuration with non-sensitive data only
    # Secrets will be fetched by DAG from API using service authentication
    # For cross-tenant invitations by platform admin, use Platform tenant's email config
    node_conf = {
        "invitation_id": db_invitation.id,
        "tenant_id": email_tenant_id,  # Use email_tenant_id to fetch correct email config
        "dashboard_url": os.getenv("DASHBOARD_URL", "http://dashboard.localhost"),
    }

    # 7. Send invitation email directly via the mailer (BUG-1027i: skip for CE when no config)
    if not ce_no_mail:
        from utils.email_sender import send_invitation_email
        background_tasks.add_task(
            send_invitation_email,
            db=db,
            invitation_id=db_invitation.id,
            tenant_id=email_tenant_id,
        )
    else:
        # BUG-1027i: CE with no email config — the row exists but nothing is sent.
        # Log the id only; never the email address (S2/S4).
        logger.info(
            "invitation %s created without email delivery (CE, email not configured)",
            db_invitation.id,
        )

    # 8. 2400LR-BUG-1027b — CE team invites: SMTP is optional/off on CE, so the
    # invite email may never arrive and the invitee has no other way in. Hand the
    # CREATING ADMIN the accept link on the create response only. The base URL is
    # the same server-side DASHBOARD_URL the mailer uses (utils/email_sender.py:543
    # builds `{DASHBOARD_URL}/accept-invitation.html?token=`; the service already
    # passes this same env value into node_conf above) — never a request header.
    # SECURITY (M1/M5): the token appears ONLY here, in this one response, to the
    # caller who holds INVITATION:create. Never in lists, by-id/by-token reads,
    # logs or audit text. The ORM row is NOT mutated — a plain dict response.
    dashboard_url = os.getenv("DASHBOARD_URL", "http://dashboard.localhost")
    created = schemas.InvitationCreated.model_validate(db_invitation)
    # FIX-INVITATIONS-EMAIL-DELIVERED-FIELD (REV 7ffe9dff26 option 1): the create
    # response now carries the DELIVERY signal the Dashboard needs — True where the
    # email was queued above (:436 `if not ce_no_mail`), False where the CE no-mail
    # branch skipped it (:400 ce_no_mail). Additive only: what is sent and logged
    # above is unchanged; paid keeps its original wording (ce_no_mail is always
    # False there, so paid always reports True).
    return created.model_copy(
        update={
            "accept_link": f"{dashboard_url.rstrip('/')}/accept-invitation.html?token={db_invitation.token}",
            "email_delivered": not ce_no_mail,
        }
    )


# =============================================================================
# GET /invitations/
# =============================================================================


def list_pending_invitations(
    db: Session,
    current_user: models.User,
    *,
    tenant_id: Optional[int],
    limit: int,
    offset: int,
    sort_by: str,
    order: str,
) -> List[models.Invitation]:
    """List pending invitations for the caller's tenant. Body of
    `list_pending_invitations_for_tenant` (routers/invitations.py).

    NOTE: `tenant_id` is accepted (kept for API-shape compatibility) but
    ignored — pre-existing, intentional: "Tenant-scoped for everyone (ISO/DoD
    tenant isolation): no cross-tenant view," even for platform admins."""
    # Tenant-scoped for everyone (ISO/DoD tenant isolation): no cross-tenant view.
    target_tenant_id = current_user.tenant_id

    return crud.get_pending_invitations_for_tenant(
        db=db, tenant_id=target_tenant_id, limit=limit, offset=offset, sort_by=sort_by, order=order
    )


# =============================================================================
# DELETE /invitations/{invitation_id}
# =============================================================================


def cancel_invitation(db: Session, current_user: models.User, invitation_id: int) -> None:
    """Cancel a pending invitation. Body of `cancel_invitation`
    (routers/invitations.py).

    Tenant-UNSCOPED fetch + explicit tenant check below (rather than a
    tenant-scoped fetch) so a cross-tenant id yields 403, not a
    misleading 404 (the migration's documented 404-vs-403 trap)."""
    # First, get the invitation without tenant filtering (exclude soft-deleted)
    db_invitation = crud_inv.get_invitation_by_id_excluding_deleted(db, invitation_id)

    if not db_invitation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")

    # Same-tenant only — no cross-tenant admin exception (ISO/DoD).
    if db_invitation.tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to cancel this invitation"
        )

    crud.delete_invitation(db=db, db_invitation=db_invitation)


# =============================================================================
# GET /private/invitations/{invitation_id}/details
# =============================================================================


def get_invitation_details(db: Session, invitation_id: int) -> dict:
    """Fetch invitation details for the service-account (worker/DAG) caller.
    Body of `get_invitation_details_by_id` (routers/invitations.py)."""
    db_invitation = crud.get_invitation_by_id(db, invitation_id=invitation_id)
    if not db_invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")

    # Extract name from email (part before @)
    recipient_name = db_invitation.email.split('@')[0] if db_invitation.email else "User"

    return {
        "recipient_email": db_invitation.email,
        "recipient_name": recipient_name,
        "invitation_token": db_invitation.token,
        "tenant_id": db_invitation.tenant_id,
        "created_at": db_invitation.created_at.isoformat() if db_invitation.created_at else None,
        "expires_at": db_invitation.expires_at.isoformat() if db_invitation.expires_at else None
    }


# =============================================================================
# GET /public/invitations/{token}
# =============================================================================


def get_invitation_by_token(db: Session, token: str) -> models.Invitation:
    """Fetch a pending invitation by token for the public accept-invite UI.
    Body of `get_invitation_details_by_token` (routers/invitations.py)."""
    db_invitation = crud.get_pending_invitation_by_token(db, token=token)
    if not db_invitation:
        raise HTTPException(status_code=404, detail="Invitation not found, has been used, or has expired.")
    return db_invitation
