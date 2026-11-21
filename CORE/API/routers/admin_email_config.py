# API/routers/admin_email_config.py
"""
launch-day-followup-5 §1A — Platform Email Configuration

Platform-admin-only endpoints for managing the transactional-email
provider configuration (Postmark in production). Replaces the prior
`.env`-based SMTP wiring that broke when Google deprecated the
Workspace App-Password path.

Architecture:
  - Operates on the `tenant_email_configs` row where tenant_id=1
    (the "Platform" tenant). This row is the platform-wide email
    config that all tenants fall back to in SaaS mode.
  - SMTP password lives in Vault at `platform/email-credentials`
    (KV v2). The mailer reads it on each send (60s in-process
    cache, invalidated on save).
  - Non-secret fields (host, port, user, sender, last-tested-at)
    live on the DB row.
  - Platform admins manage the config via the Dashboard
    System → Email tab; non-admins are blocked with a
    cross-tenant-data-access-blocked audit emit.

Vault path note: spec says `secret/email/provider-credentials`,
but the existing api-platform-config Vault policy only allows
`secret/data/platform/*`. To stay within existing policy
boundaries (no infra changes during launch week), we use
`platform/email-credentials`.
"""
# lockbox: platform-only — FX.67.s3 sweep marker: this router reads/writes
# the platform's own email configuration (SMTP settings + the platform
# Vault path), never another tenant's content, so there is no target
# tenant for require_lockbox_for_cross_tenant to check.

import os
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

import audit
import models
import schemas
from dependencies import get_db, require_active_and_onboarded_user
from services import admin_email_config_service
from utils.vault_client import VaultError, approle_login, read_kv_v2, write_kv_v2
from utils.http_errors import internal_http_error
from utils.smtp_security import (
    build_ssl_context,
    config_is_complete,
    friendly_smtp_error,
    resolve_smtp_mode,
)

logger = logging.getLogger(__name__)

# clean-v2-api (admin dissolve wave): moved from `/admin/email-config/*` to
# `/system/email/*` (design.md: `email-config/* -> system/email`). `router`
# carries no prefix — routes live on `_impl` (mounted at the canonical
# prefix below the endpoint defs). The old `/admin/email-config` (GET/POST)
# + `/admin/email-config/test` back-compat aliases for
# CORE/Dashboard/js/settings.js have been removed (final clean-cut) now that
# settings.js is repointed to `/system/email`.
router = APIRouter(tags=["System - Email Config"])
_impl = APIRouter()

# Vault KV v2 path (relative to secret/data/). Stays under the
# existing api-platform-config policy (`secret/data/platform/*`).
VAULT_EMAIL_PATH = "platform/email-credentials"

# Platform tenant id — the "Platform" tenant whose email config
# row is the system-wide default for all outbound mail.
PLATFORM_TENANT_ID = 1


# ----------------------------------------------------------------------
# Auth / Vault helpers — lifted from admin_plans_payment_settings.py.
# ----------------------------------------------------------------------

def require_platform_admin(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
) -> models.User:
    """Platform-admin gate. Non-admins get 403 + cross-tenant audit emit
    so the audit trail captures attempts to enumerate the email config.
    """
    if not current_user.is_platform_admin:
        # Emit ADMIN_CROSS_TENANT_DATA_ACCESS_BLOCKED — the user is
        # attempting to read/modify the platform tenant's data without
        # being a platform admin. Debounce + fail-open behavior is
        # handled inside the helper. Never let an audit error mask the
        # auth error.
        try:
            audit.log_cross_tenant_data_access_blocked(
                db=db,
                admin_user_id=current_user.id,
                target_tenant_id=PLATFORM_TENANT_ID,
                request=request,
                resource_type="email_config",
                endpoint_path=str(getattr(request, "url", "")) or None,
                method=getattr(request, "method", None),
            )
        except Exception:
            logger.warning("Cross-tenant block audit failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires platform admin privileges",
        )
    return current_user


def _vault_token() -> str:
    """Get Vault token: prefer Agent token (auto-renewed), fall back to AppRole login."""
    agent_token_path = "/run/vault/token"
    if os.path.exists(agent_token_path):
        with open(agent_token_path) as f:
            token = f.read().strip()
        if token:
            return token
    role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if not role_id or not secret_id:
        raise VaultError("Vault not available: no Agent token and no AppRole configured")
    return approle_login(role_id=role_id, secret_id=secret_id)


def _read_email_creds_from_vault() -> dict:
    """Read SMTP creds dict from Vault. Empty dict on miss."""
    try:
        token = _vault_token()
        return read_kv_v2(VAULT_EMAIL_PATH, token=token)
    except VaultError:
        return {}
    except Exception as e:
        logger.warning("Could not read email credentials from Vault: %s", e)
        return {}


def _write_email_creds_to_vault(data: dict) -> None:
    """Write SMTP creds dict to Vault."""
    token = _vault_token()
    write_kv_v2(VAULT_EMAIL_PATH, token=token, data=data)


def _get_or_create_platform_email_row(db: Session) -> models.TenantEmailConfig:
    """Return the platform tenant's TenantEmailConfig row, creating it if missing.

    Gate 2b migration: the query moved to `crud/admin_email_config.py`, the
    get-or-create policy to `services/admin_email_config_service.py`. Kept as
    a thin wrapper here (rather than repointing every call site) since
    nothing external imports or patches this name.
    """
    return admin_email_config_service.get_or_create_platform_email_row(db, PLATFORM_TENANT_ID)


def _serialize(row: models.TenantEmailConfig, vault_data: dict) -> schemas.PlatformEmailConfigResponse:
    """Build the response. Never includes the password itself.

    Gate 2b migration: pure response-shaping, moved to
    `services/admin_email_config_service.py`. Kept as a thin wrapper here
    since nothing external imports or patches this name.
    """
    return admin_email_config_service.serialize_config(row, vault_data)


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------
# FX.67.s2 (Q50=A) — deliberately UNGATED by the lockbox cross-tenant
# gate: every endpoint here reads/writes only the PLATFORM tenant's own
# email-config row (tenant_id=1, the staff tenant the caller lives in)
# plus the Vault path `platform/email-credentials` — no other tenant's
# content is surfaced, so there is no foreign tenant id to check.

@_impl.get("", response_model=schemas.PlatformEmailConfigResponse)
def get_email_config(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_platform_admin),
):
    """Read the platform email configuration. Password never returned —
    only `password_set` / `is_configured` flags so the UI can show
    "Password set, leave blank to keep" without revealing the value.
    """
    row = _get_or_create_platform_email_row(db)
    vault_data = _read_email_creds_from_vault() if row.password_vault_path else {}
    return _serialize(row, vault_data)


@_impl.post("", response_model=schemas.PlatformEmailConfigResponse)
def update_email_config(
    update_data: schemas.PlatformEmailConfigUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_platform_admin),
):
    """Update the platform email config.

    Password handling:
      - None (omitted)  → preserve existing Vault value
      - "" (empty)      → clear / unconfigure (deletes Vault key, clears
                          legacy column)
      - non-empty       → write to Vault at `platform/email-credentials`,
                          set password_vault_path, clear legacy column
    Mailer cache is invalidated for tenant 1 so the next send picks up
    the new value without an API restart.
    """
    row = _get_or_create_platform_email_row(db)
    changed_fields = []

    # Non-secret field updates — only fields explicitly supplied are
    # touched. `model_dump(exclude_unset=True)` would also work but
    # we want to track which fields actually changed for the audit
    # event details.
    for field in ("provider", "smtp_host", "smtp_port", "smtp_user", "sender_name", "sender_email"):
        new_value = getattr(update_data, field, None)
        if new_value is not None:
            old = getattr(row, field, None)
            # Convert EmailStr to str for storage / comparison.
            new_str = str(new_value) if new_value is not None else None
            if str(old) != new_str:
                setattr(row, field, new_str)
                changed_fields.append(field)

    # Explicit transport security (schema validated; 'auto' is stored as NULL so
    # the historical port rule applies) + self-signed opt-in. None = preserve.
    if update_data.smtp_security is not None:
        new_sec = None if update_data.smtp_security == "auto" else update_data.smtp_security
        if row.smtp_security != new_sec:
            row.smtp_security = new_sec
            changed_fields.append("smtp_security")
    if update_data.smtp_allow_selfsigned is not None:
        if bool(row.smtp_allow_selfsigned) != update_data.smtp_allow_selfsigned:
            row.smtp_allow_selfsigned = update_data.smtp_allow_selfsigned
            changed_fields.append("smtp_allow_selfsigned")

    # Password handling — three-way semantics.
    pw_changed = False
    if update_data.smtp_password is not None:
        if update_data.smtp_password == "":
            # Clear: delete from Vault if present, clear legacy column.
            if row.password_vault_path:
                try:
                    vault_data = _read_email_creds_from_vault()
                    if "smtp_password" in vault_data:
                        vault_data.pop("smtp_password", None)
                        _write_email_creds_to_vault(vault_data)
                except VaultError as e:
                    raise internal_http_error(503, "Email password could not be cleared from Vault", e) from e
            row.password_vault_path = None
            row.encrypted_smtp_password = None
            pw_changed = True
            changed_fields.append("smtp_password_cleared")
        else:
            # Write: store in Vault, mark vault path on the row, clear
            # the legacy encrypted column (migration-to-Vault complete).
            try:
                vault_data = _read_email_creds_from_vault()
                vault_data["smtp_password"] = update_data.smtp_password
                _write_email_creds_to_vault(vault_data)
            except VaultError as e:
                raise internal_http_error(503, "Email password could not be saved to Vault", e) from e
            row.password_vault_path = VAULT_EMAIL_PATH
            row.encrypted_smtp_password = None
            pw_changed = True
            changed_fields.append("smtp_password_set")

    db.commit()
    db.refresh(row)

    # Invalidate mailer cache so the next send reads from the fresh
    # config. Lazy import to avoid pulling email_sender in module-load
    # ordering edge cases at app startup.
    try:
        from utils.email_sender import invalidate_email_env_cache
        invalidate_email_env_cache(PLATFORM_TENANT_ID)
    except Exception:
        logger.warning("Failed to invalidate mailer cache after config save", exc_info=True)

    # Push to Keycloak realm SMTP so password-reset / verification emails
    # send through the same provider. Only when we have a complete config
    # (host + user + password + sender). Failure is non-fatal — admin can
    # see the result via the test-send button, and the MOD-side config is
    # still saved.
    #
    # Audit finding #3 follow-up (2026-06-04): once SMTP is wired the
    # `verifyEmail` required action becomes safe to enable, so we flip
    # it on in the same handler. Couples the two settings that have to
    # move together — without SMTP the realm can't send verify mails,
    # without verify=true the SMTP wiring buys you nothing on signup.
    # `start-main-server.sh` deliberately defers the flag on a fresh
    # install; this save path is the place where verifyEmail goes on
    # automatically, no `make require-email-verify` step needed.
    #
    # An explicit Encryption choice also allows an unauthenticated server (local
    # relay / smtp-relay.gmail.com): host + sender, no user/password. The realm
    # gets the same auth/starttls/ssl rule as the mailer (utils/smtp_security).
    # Keycloak keeps its own trust store, so "Allow self-signed" does not apply to
    # the realm sync: a private-CA relay must be trusted inside Keycloak itself.
    kc_user_ok = bool(row.smtp_user) or (row.smtp_security is not None)
    if row.smtp_host and kc_user_ok and row.sender_email:
        try:
            vault_data_for_kc = _read_email_creds_from_vault() if row.password_vault_path else {}
            kc_password = vault_data_for_kc.get("smtp_password")
            if kc_password or not row.smtp_user:
                from keycloak_auth import keycloak_admin_client
                smtp_ok = keycloak_admin_client.update_realm_smtp(
                    smtp_host=row.smtp_host,
                    smtp_port=int(row.smtp_port or 587),
                    smtp_user=row.smtp_user,
                    smtp_password=kc_password,
                    from_email=row.sender_email,
                    smtp_security=row.smtp_security,
                )
                if smtp_ok:
                    ve_ok, ve_status, ve_detail = keycloak_admin_client.set_verify_email(True)
                    if ve_ok:
                        logger.info(
                            "Realm verifyEmail auto-enabled after SMTP save by user_id=%s",
                            current_user.id,
                        )
                        changed_fields.append("realm_verify_email_enabled")
                    else:
                        logger.warning(
                            "SMTP synced to realm but verifyEmail auto-enable failed "
                            "(status=%s detail=%s) — operator can flip manually via "
                            "`make require-email-verify`",
                            ve_status, ve_detail,
                        )

                    # Now that SMTP can send mail, turn on the self-service
                    # 'Forgot Password?' flow so tenant users can reset their
                    # own passwords. Coupled to the SMTP wiring: a reset link
                    # without a working mailer is a dead end, so this enables
                    # in lockstep with update_realm_smtp succeeding.
                    rp_ok, rp_status, rp_detail = keycloak_admin_client.set_reset_password_allowed(True)
                    if rp_ok:
                        logger.info(
                            "Realm resetPasswordAllowed auto-enabled after SMTP save by user_id=%s",
                            current_user.id,
                        )
                        changed_fields.append("realm_reset_password_enabled")
                    else:
                        logger.warning(
                            "SMTP synced to realm but resetPasswordAllowed auto-enable "
                            "failed (status=%s detail=%s)",
                            rp_status, rp_detail,
                        )
        except Exception as e:
            logger.warning("Email saved to MOD but failed to sync to Keycloak realm SMTP: %s", e)

    # Audit emit — never raise on audit failure.
    try:
        db.add(models.AuditEvent(
            tenant_id=current_user.tenant_id,
            user_id=current_user.id,
            event_name="ADMIN_EMAIL_CONFIG_UPDATED",
            details={
                "changed_fields": changed_fields,
                "password_changed": pw_changed,
                "vault_path": row.password_vault_path,
            },
        ))
        db.commit()
    except Exception:
        logger.warning("Audit emit for ADMIN_EMAIL_CONFIG_UPDATED failed", exc_info=True)

    vault_data = _read_email_creds_from_vault() if row.password_vault_path else {}
    return _serialize(row, vault_data)


@_impl.post("/test", response_model=schemas.EmailTestResponse)
def test_email_config(
    payload: schemas.EmailTestRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_platform_admin),
):
    """Send a test message via the saved config. Updates last_tested_at
    + last_test_result on success/failure so the page can show the
    last outcome. Audit emit on every attempt.

    Test-failure semantics: the configuration is NOT cleared on failure
    — admin can correct the credential and retry. Failure reason is
    surfaced verbatim from the SMTP exception (truncated to 500 chars).
    """
    row = _get_or_create_platform_email_row(db)

    # Resolve password the same way the mailer does — Vault first,
    # legacy encrypted column second.
    vault_data = _read_email_creds_from_vault() if row.password_vault_path else {}
    password = vault_data.get("smtp_password")
    if not password and row.encrypted_smtp_password:
        try:
            from crud.smtp_cud import get_decrypted_email_config
            cfg = get_decrypted_email_config(db, tenant_id=PLATFORM_TENANT_ID)
            if cfg:
                password = cfg.get("smtp_password")
        except Exception:
            pass

    if not config_is_complete(row.smtp_host, row.smtp_user, password, row.sender_email, row.smtp_security):
        result = schemas.EmailTestResponse(
            success=False,
            error=(
                "Email config is incomplete — host, user, password, and sender_email are all required"
                if row.smtp_security is None
                else "Email config is incomplete — host and sender_email are required "
                     "(and a password whenever a user is set)"
            ),
        )
        row.last_tested_at = datetime.now(timezone.utc)
        row.last_test_result = "failed"
        row.last_test_error = result.error
        db.commit()
        _emit_test_audit(db, current_user, payload.recipient_email, result)
        return result

    # Inline SMTP send. Same logic shape as utils.email_sender._send_email_smtp
    # but specialized for the test path so we can return the provider
    # response id (Postmark surfaces a Message-ID header).
    import smtplib
    from email.mime.text import MIMEText
    from email.utils import formataddr, make_msgid

    msg = MIMEText(
        "This is a test message from MOD Core's platform email "
        "configuration. If you received this, your provider settings "
        "are working.\n\n"
        "Sent at: " + datetime.now(timezone.utc).isoformat(),
        "plain",
        "utf-8",
    )
    msg["Subject"] = "MOD Platform — Email config test"
    msg["From"] = formataddr((row.sender_name or "MOD Platform", row.sender_email))
    msg["To"] = payload.recipient_email
    msg["Message-ID"] = make_msgid(domain=(row.sender_email.split("@")[-1] if "@" in row.sender_email else "example.com"))

    port = int(row.smtp_port or 587)
    mode = resolve_smtp_mode(port, row.smtp_security)
    ctx = build_ssl_context(bool(row.smtp_allow_selfsigned))
    error: Optional[str] = None
    try:
        if mode == "ssl":
            with smtplib.SMTP_SSL(row.smtp_host, port, timeout=20, context=ctx) as smtp:
                if row.smtp_user:
                    smtp.login(row.smtp_user, password or "")
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(row.smtp_host, port, timeout=20) as smtp:
                smtp.ehlo()
                if mode == "starttls":
                    smtp.starttls(context=ctx)
                    smtp.ehlo()
                if row.smtp_user:
                    smtp.login(row.smtp_user, password or "")
                smtp.send_message(msg)
    except Exception as e:
        # One-sentence advice for the common failures; the raw server reply is
        # appended (scrubbed of the password) so nothing is hidden.
        error = friendly_smtp_error(
            e, row.smtp_host, port, mode, secrets=[password]
        )

    error = error[:500] if error else None
    success = error is None

    row.last_tested_at = datetime.now(timezone.utc)
    row.last_test_result = "success" if success else "failed"
    row.last_test_error = None if success else error
    db.commit()

    result = schemas.EmailTestResponse(
        success=success,
        provider_message_id=msg.get("Message-ID") if success else None,
        error=error,
    )
    _emit_test_audit(db, current_user, payload.recipient_email, result)
    return result


def _emit_test_audit(
    db: Session,
    actor: models.User,
    recipient: str,
    result: schemas.EmailTestResponse,
) -> None:
    """Audit emit for the test-send button. Never raises.

    Gate 2b migration: moved to `services/admin_email_config_service.py`
    (plain AuditEvent insert, no external call). Kept as a thin wrapper here
    since nothing external imports or patches this name.
    """
    admin_email_config_service.emit_test_audit(db, actor, recipient, result)


router.include_router(_impl, prefix="/system/email")
