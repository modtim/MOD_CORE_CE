"""SMTP Settings CRUD operations - SMTP settings management."""
from typing import Optional
import os
import logging

from sqlalchemy.orm import Session
import models, schemas
from crud.secret_cud import (
    create_or_update_secret,
    get_decrypted_secret_by_name,
    get_secret_by_name,
    delete_secret,
)

logger = logging.getLogger(__name__)

# SMTP password is stored as a tenant secret with this fixed name
_SMTP_PASSWORD_SECRET = "smtp_password"


def get_smtp_settings_by_user_id(db: Session, user_id: int) -> Optional[models.SMTPSettings]:
    """Retrieve SMTP settings for a user."""
    return db.query(models.SMTPSettings).filter(models.SMTPSettings.user_id == user_id).first()


def create_smtp_settings(
    db: Session,
    smtp_settings: schemas.SMTPSettingsCreate,
    user_id: int
) -> models.SMTPSettings:
    """Create new SMTP settings for a user.

    SF1/SF2: SMTP password stored via tenant secret system (Vault + correct tenant key).
    """
    # Store password through Vault-backed secret system
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user and smtp_settings.smtp_password:
        create_or_update_secret(db, _SMTP_PASSWORD_SECRET, smtp_settings.smtp_password, user, description="SMTP password")

    payload = smtp_settings.dict(exclude={"smtp_password"})

    # iso-soft-delete-sweep (2026-09-22): `user_id` is UNIQUE (plain constraint),
    # so a soft-deleted row still owns the slot. On re-create for a user whose
    # prior settings were soft-deleted, reactivate the tombstone in place rather
    # than colliding on INSERT. (The soft-delete listener hides it from the
    # normal per-user lookup, so query WITH tombstones here.)
    existing = (
        db.query(models.SMTPSettings)
        .execution_options(include_deleted=True)
        .filter(models.SMTPSettings.user_id == user_id)
        .first()
    )
    if existing is not None and existing.deleted_at is not None:
        for key, value in payload.items():
            setattr(existing, key, value)
        existing.encrypted_smtp_password = b"vault"
        existing.deleted_at = None
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing

    db_smtp_settings = models.SMTPSettings(
        **payload,
        encrypted_smtp_password=b"vault",  # marker: real value in Vault
        user_id=user_id
    )
    db.add(db_smtp_settings)
    db.commit()
    db.refresh(db_smtp_settings)
    return db_smtp_settings


def update_smtp_settings(
    db: Session,
    smtp_settings_id: int,
    smtp_settings_update: schemas.SMTPSettingsUpdate
) -> Optional[models.SMTPSettings]:
    """Update existing SMTP settings.

    SF1/SF2: SMTP password stored via tenant secret system (Vault + correct tenant key).
    """
    db_smtp_settings = db.query(models.SMTPSettings).filter(models.SMTPSettings.id == smtp_settings_id).first()
    if not db_smtp_settings:
        return None

    update_data = smtp_settings_update.dict(exclude_unset=True)
    if "smtp_password" in update_data and update_data["smtp_password"]:
        # Route password through Vault-backed secret system
        user = db.query(models.User).filter(models.User.id == db_smtp_settings.user_id).first()
        if user:
            create_or_update_secret(db, _SMTP_PASSWORD_SECRET, update_data["smtp_password"], user, description="SMTP password")
        db_smtp_settings.encrypted_smtp_password = b"vault"  # marker
        del update_data["smtp_password"]

    for key, value in update_data.items():
        setattr(db_smtp_settings, key, value)

    db.commit()
    db.refresh(db_smtp_settings)
    return db_smtp_settings


def delete_smtp_settings(db: Session, smtp_settings_id: int):
    """Soft-delete SMTP settings.

    iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    iso-no-hard-delete). `SMTPSettings` now carries a `deleted_at` column
    (SoftDeleteMixin) and is in database.py's soft-delete listener, so the
    lookup below only sees live rows and the tombstone leaves every read.
    `create_smtp_settings` reactivates the tombstone on re-create.
    """
    from datetime import datetime, timezone
    db_smtp_settings = db.query(models.SMTPSettings).filter(models.SMTPSettings.id == smtp_settings_id).first()
    if db_smtp_settings:
        db_smtp_settings.deleted_at = datetime.now(timezone.utc)
        db.commit()
    return db_smtp_settings


def get_email_config(db: Session, tenant_id: int) -> models.TenantEmailConfig:
    """Retrieve email configuration for a tenant."""
    return db.query(models.TenantEmailConfig).filter(
        models.TenantEmailConfig.tenant_id == tenant_id
    ).first()


def create_or_update_email_config(
    db: Session,
    tenant_id: int,
    config: schemas.EmailConfigUpdate,
    user: models.User = None,
) -> models.TenantEmailConfig:
    """Create or update email configuration for a tenant.

    If *user* is provided, SMTP password and provider secrets are stored
    through the tenant-secret system (Vault + DB with correct tenant key).
    """
    existing = db.query(models.TenantEmailConfig).filter(
        models.TenantEmailConfig.tenant_id == tenant_id
    ).first()

    if existing:
        existing.provider = config.provider
        existing.sender_name = config.sender_name
        existing.sender_email = config.sender_email
        existing.smtp_host = config.smtp_host
        existing.smtp_port = config.smtp_port
        existing.smtp_user = config.smtp_user
        # Store SMTP password via tenant secret system (Vault + DB)
        if config.smtp_password and user:
            create_or_update_secret(db, _SMTP_PASSWORD_SECRET, config.smtp_password, user, description="SMTP password")
            existing.encrypted_smtp_password = b"vault"  # marker: real value is in Vault
        db.commit()
        db.refresh(existing)
        if user:
            _manage_provider_secrets(db, tenant_id, config.provider, user)
        return existing

    db_config = models.TenantEmailConfig(
        tenant_id=tenant_id,
        provider=config.provider,
        sender_name=config.sender_name,
        sender_email=config.sender_email,
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        smtp_user=config.smtp_user,
        encrypted_smtp_password=None,
    )
    # Store SMTP password via tenant secret system (Vault + DB)
    if config.smtp_password and user:
        create_or_update_secret(db, _SMTP_PASSWORD_SECRET, config.smtp_password, user, description="SMTP password")
        db_config.encrypted_smtp_password = b"vault"  # marker
    db.add(db_config)
    db.commit()
    db.refresh(db_config)
    if user:
        _manage_provider_secrets(db, tenant_id, config.provider, user)
    return db_config


def _manage_provider_secrets(db: Session, tenant_id: int, provider: str, user: models.User):
    """Manage placeholder secrets based on selected provider.

    Routes through secret_cud so secrets go through Vault with correct tenant key.
    """
    if provider == 'mailgun':
        _ensure_secret_exists(db, user, "mailgun_api_key", "CONFIGURE ME: Mailgun API Key for invitations")
        _ensure_secret_exists(db, user, "mailgun_domain", "CONFIGURE ME: Mailgun Domain for invitations")
    elif provider == 'brevo':
        _ensure_secret_exists(db, user, "brevo_api_key", "CONFIGURE ME: Brevo API Key for invitations")
        _ensure_secret_exists(db, user, "brevo_template_id", "CONFIGURE ME: Brevo Template ID for invitations")


_PLATFORM_TENANT_ID = 1


def _read_smtp_password_from_vault_path(vault_path: str) -> str:
    """Read an SMTP password from an OpenBao KV v2 path (a row's
    password_vault_path, e.g. the platform row's platform/email-credentials).
    Returns '' on any failure."""
    try:
        from utils.vault_client import get_vault_token, read_kv_v2
        token = get_vault_token()
        if not token:
            return ""
        data = read_kv_v2(vault_path, token=token) or {}
        return data.get("smtp_password") or data.get("password") or ""
    except Exception:
        return ""


def get_decrypted_email_config(db: Session, tenant_id: int) -> dict:
    """Retrieve email configuration with decrypted password for a tenant.

    - Platform-originated emails (usage alerts, invoices, welcome) fall back to
      the platform email config (tenant 1) when the recipient tenant hasn't
      configured its own SMTP.
    - The password is read from the row's ``password_vault_path`` when set
      (this supersedes the legacy tenant-secret resolution) — that's how the
      platform row's password at ``platform/email-credentials`` is found.
    """
    config = db.query(models.TenantEmailConfig).filter(
        models.TenantEmailConfig.tenant_id == tenant_id
    ).first()
    effective_tenant_id = tenant_id
    if not config and tenant_id != _PLATFORM_TENANT_ID:
        config = db.query(models.TenantEmailConfig).filter(
            models.TenantEmailConfig.tenant_id == _PLATFORM_TENANT_ID
        ).first()
        effective_tenant_id = _PLATFORM_TENANT_ID
    if not config:
        return None

    vault_path = getattr(config, "password_vault_path", None)
    if vault_path:
        smtp_password = _read_smtp_password_from_vault_path(vault_path)
    else:
        # Legacy: tenant secret system (Vault with DB fallback).
        smtp_password = get_decrypted_secret_by_name(db, _SMTP_PASSWORD_SECRET, effective_tenant_id)

    return {
        "provider": config.provider,
        "smtp_host": config.smtp_host,
        "smtp_port": config.smtp_port,
        "smtp_user": config.smtp_user,
        "smtp_password": smtp_password,
        "sender_email": config.sender_email,
        "sender_name": config.sender_name,
        # NULL = auto (legacy port rule); see utils/smtp_security.py
        "smtp_security": getattr(config, "smtp_security", None),
        "smtp_allow_selfsigned": bool(getattr(config, "smtp_allow_selfsigned", False)),
    }


def _ensure_secret_exists(db: Session, user: models.User, secret_name: str, description: str):
    """Ensure a secret exists for the tenant. Routes through secret_cud for Vault + correct key."""
    existing = get_secret_by_name(db, secret_name=secret_name, tenant_id=user.tenant_id)
    if existing:
        return existing

    return create_or_update_secret(
        db,
        secret_name=secret_name,
        value="Not Configured",
        user=user,
        description=description,
    )


def _remove_secret_if_exists(db: Session, tenant_id: int, secret_name: str):
    """Remove a secret if it exists. Routes through secret_cud for Vault cleanup."""
    secret = get_secret_by_name(db, secret_name=secret_name, tenant_id=tenant_id)
    if secret:
        delete_secret(db, secret)
    return secret
