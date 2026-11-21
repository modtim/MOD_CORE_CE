"""Secret CRUD operations - Secret management within tenants."""
import os
import logging
from typing import Optional, List

from fastapi import HTTPException
from sqlalchemy.orm import Session
from datetime import datetime

import models, schemas
from utils import encryption
from utils.vault_client import VaultError, approle_login, read_kv_v2, write_kv_v2, delete_kv_v2

from services import tenant_key_local
from utils.paid_import import optional_paid

# Keep the name `key_provider` — tests monkeypatch it. In the Paid tree this
# IS services.key_provider (vault/pkcs11/kmip backends included); in the CE
# carve it falls back to the shipped local-only module, byte-compatible.
key_provider = optional_paid("services.key_provider") or tenant_key_local

logger = logging.getLogger(__name__)


def _unwrap_tenant_key(wrapped_envelope: str, tenant_id: int, db=None) -> str:
    """Unwrap via the resolved key provider, turning a Paid-backend envelope
    (vault/pkcs11/kmip) into the upgrade-path 402 the rest of the API uses."""
    try:
        return key_provider.unwrap_tenant_key(wrapped_envelope, tenant_id, db=db)
    except tenant_key_local.PaidKeyBackendRequired:
        raise HTTPException(
            status_code=402,
            detail="This tenant's key was wrapped by a key backend "
                   "(Vault transit / PKCS#11 / KMIP), which is not available "
                   "in MOD Community Edition.",
        )


def _vault_service_token() -> str:
    role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if not role_id or not secret_id:
        raise VaultError("Vault AppRole is not configured for API")
    return approle_login(role_id=role_id, secret_id=secret_id)


def _vault_tenant_secret_path(tenant_id: int, secret_name: str) -> str:
    safe = (secret_name or "").strip()
    return f"tenants/{tenant_id}/secrets/{safe}"


def _vault_put_tenant_secret(tenant_id: int, secret_name: str, value: str) -> None:
    token = _vault_service_token()
    write_kv_v2(_vault_tenant_secret_path(tenant_id, secret_name), token=token, data={"value": value})


def _vault_get_tenant_secret(tenant_id: int, secret_name: str) -> Optional[str]:
    token = _vault_service_token()
    data = read_kv_v2(_vault_tenant_secret_path(tenant_id, secret_name), token=token)
    val = data.get("value")
    if val is None:
        return None
    return str(val)


def _vault_delete_tenant_secret(tenant_id: int, secret_name: str) -> None:
    token = _vault_service_token()
    delete_kv_v2(_vault_tenant_secret_path(tenant_id, secret_name), token=token)


def _ensure_tenant_encryption_key(db: Session, tenant: "models.Tenant") -> str:
    """Return the tenant's encrypted-key material, generating and persisting
    one first if the tenant was provisioned through a path that never set
    it (e.g. the startup bootstrap tenant in database.py:seed_service_user,
    or an older onboarding path). Mirrors the self-heal create_tenant.py
    already does for the platform tenant, so every tenant -- not just
    tenant 1 -- gets the same recovery instead of a hard 500 on every
    secret create forever.
    """
    if not tenant.encryption_key:
        # Lock + re-check: concurrent first creates (Settings auto-creates
        # several placeholders at once) must not each mint a key — the loser's
        # overwrite would leave the winner's secret undecryptable.
        locked = (
            db.query(models.Tenant)
            .filter(models.Tenant.id == tenant.id)
            .with_for_update()
            .populate_existing()  # re-read the row, not the stale identity-map copy
            .one()
        )
        if not locked.encryption_key:
            _plaintext, locked.encryption_key = key_provider.generate_and_wrap_tenant_key(
                locked.id, db=db)
            logger.warning(
                "Tenant %s had no encryption key; generated one on first secret create.",
                tenant.id,
            )
        db.commit()
        db.refresh(tenant)
    return tenant.encryption_key


def create_secret(
    db: Session,
    secret: schemas.SecretCreate,
    user: models.User
) -> models.Secret:
    """
    Encrypts a secret's value using the tenant-specific key and saves it.
    If a soft-deleted secret with the same name exists, it will be restored with new values.
    """
    # 1. Retrieve (or backfill) the tenant's encrypted key
    tenant = user.tenant
    tenant_encryption_key = _ensure_tenant_encryption_key(db, tenant)

    # 2. Unwrap the tenant's key (local Fernet or Vault transit, per the
    #    envelope's own format — see services.key_provider)
    decrypted_tenant_key = _unwrap_tenant_key(tenant_encryption_key, tenant.id, db=db)

    # 3. Encrypt the new secret's value with the decrypted tenant key
    encrypted_value = encryption.encrypt_value(secret.value, decrypted_tenant_key)

    # 3b. Store plaintext secret in Vault (tenant-scoped path). If Vault write fails, fail closed.
    _vault_put_tenant_secret(user.tenant_id, secret.name, secret.value)

    # 4. Check if a soft-deleted secret with this name exists
    existing_deleted = (
        db.query(models.Secret)
        .filter(
            models.Secret.name == secret.name,
            models.Secret.tenant_id == user.tenant_id,
            models.Secret.deleted_at.isnot(None)
        )
        .first()
    )

    if existing_deleted:
        # Restore the soft-deleted secret with new values
        existing_deleted.encrypted_value = encrypted_value
        existing_deleted.description = secret.description
        existing_deleted.deleted_at = None
        existing_deleted.created_by_user_id = user.id
        db.commit()
        db.refresh(existing_deleted)
        return existing_deleted

    # 5. Create new secret
    db_secret = models.Secret()
    db_secret.name = secret.name
    db_secret.description = secret.description
    db_secret.encrypted_value = encrypted_value
    db_secret.tenant_id = user.tenant_id
    db_secret.created_by_user_id = user.id
    db.add(db_secret)
    db.commit()
    db.refresh(db_secret)
    return db_secret


def get_decrypted_secret(
    db: Session,
    secret_id: int,
    tenant_id: int
) -> Optional[str]:
    """Retrieve and decrypt a secret's value from Vault.

    Raises VaultError if Vault is unavailable -- no DB fallback.
    """
    db_secret = get_secret_by_id(db, secret_id=secret_id, tenant_id=tenant_id)
    if not db_secret:
        return None

    return _vault_get_tenant_secret(tenant_id=tenant_id, secret_name=db_secret.name)


def get_secrets_for_tenant(
    db: Session,
    tenant_id: int,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "name",
    order: str = "asc",
) -> List[models.Secret]:
    """Retrieve a paginated list of secrets for a specific tenant."""
    query = db.query(models.Secret).filter(
        models.Secret.tenant_id == tenant_id,
        models.Secret.deleted_at.is_(None)
    )

    # S7: Validate sort_by against allowlist to prevent arbitrary column access
    allowed_sort_columns = {"name", "created_at", "description", "updated_at"}
    if sort_by in allowed_sort_columns and hasattr(models.Secret, sort_by):
        column = getattr(models.Secret, sort_by)
        if order.lower() == "desc":
            query = query.order_by(column.desc())
        else:
            query = query.order_by(column.asc())

    return query.offset(offset).limit(limit).all()


def get_secret_by_id(
    db: Session,
    secret_id: int,
    tenant_id: int
) -> Optional[models.Secret]:
    """Retrieve a single secret by its ID, ensuring it belongs to the correct tenant."""
    return (
        db.query(models.Secret)
        .filter(
            models.Secret.id == secret_id,
            models.Secret.tenant_id == tenant_id,
            models.Secret.deleted_at.is_(None)
        )
        .first()
    )


def get_secret_by_name(
    db: Session,
    secret_name: str,
    tenant_id: int
) -> Optional[models.Secret]:
    """Retrieve a single secret by its name, ensuring it belongs to the correct tenant."""
    return (
        db.query(models.Secret)
        .filter(
            models.Secret.name == secret_name,
            models.Secret.tenant_id == tenant_id,
            models.Secret.deleted_at.is_(None)
        )
        .first()
    )


def get_decrypted_secret_by_name(
    db: Session,
    secret_name: str,
    tenant_id: int
) -> Optional[str]:
    """Retrieve and decrypt a secret's value by name from Vault.

    Raises VaultError if Vault is unavailable -- no DB fallback.
    """
    db_secret = get_secret_by_name(db, secret_name=secret_name, tenant_id=tenant_id)
    if not db_secret:
        return None

    return _vault_get_tenant_secret(tenant_id=tenant_id, secret_name=secret_name)


def resolve_secret_for_tenant(
    db: Session,
    secret_name: str,
    tenant_id: int
) -> Optional[models.Secret]:
    """Resolve a secret by name for a tenant.

    Checks in order:
    1. Tenant's own secret (tenant_id matches)
    2. Platform secret shared with this specific tenant
    3. Platform secret shared with all tenants

    Returns the Secret record (not decrypted). Use the returned secret's
    tenant_id to look up the Vault path for decryption.
    """
    tenant_secret = get_secret_by_name(db, secret_name=secret_name, tenant_id=tenant_id)
    if tenant_secret:
        return tenant_secret

    tid_str = str(tenant_id)

    shared_specific = (
        db.query(models.Secret)
        .filter(
            models.Secret.name == secret_name,
            models.Secret.tenant_id.is_(None),
            models.Secret.deleted_at.is_(None),
        )
        .all()
    )
    for s in shared_specific:
        ids = s.shared_with_tenant_ids or []
        if tid_str in ids:
            return s

    shared_all = (
        db.query(models.Secret)
        .filter(
            models.Secret.name == secret_name,
            models.Secret.tenant_id.is_(None),
            models.Secret.deleted_at.is_(None),
            models.Secret.shared_with_all_tenants == True,
        )
        .first()
    )
    if shared_all:
        return shared_all

    return None


def resolve_decrypted_secret_for_tenant(
    db: Session,
    secret_name: str,
    tenant_id: int
) -> Optional[str]:
    """Resolve and decrypt a secret for a tenant, checking shared platform secrets."""
    secret = resolve_secret_for_tenant(db, secret_name=secret_name, tenant_id=tenant_id)
    if not secret:
        return None

    source_tenant_id = secret.tenant_id if secret.tenant_id is not None else 0
    return _vault_get_tenant_secret(tenant_id=source_tenant_id, secret_name=secret_name)


def update_secret(
    db: Session,
    db_secret: models.Secret,
    secret_update: schemas.SecretUpdate
) -> models.Secret:
    """Update a secret's name, description, or re-encrypt its value."""
    update_data = secret_update.dict(exclude_unset=True)

    # FX.117 PART 2 — scope/expiry are applied by the service layer
    # (services/secrets_service.update_secret) with validation; the
    # service passes a copy of the payload without these keys, so they
    # are dropped here too (defence in depth — a raw datetime string
    # must never be setattr'd on a DateTime column).
    update_data.pop("allowed_workflow_ids", None)
    update_data.pop("expires_at", None)

    # Track old name for Vault path moves
    old_name = db_secret.name

    for key, value in update_data.items():
        if key == "value":
            # Update Vault first (fail closed)
            _vault_put_tenant_secret(db_secret.tenant_id, db_secret.name, value)

            # Maintain legacy DB encrypted_value for backward compatibility.
            # Backfill the tenant key if missing (legacy/bootstrap tenants) so
            # the edit is never silently dropped.
            tenant = db.query(models.Tenant).filter(models.Tenant.id == db_secret.tenant_id).first()
            if tenant:
                tenant_key = _ensure_tenant_encryption_key(db, tenant)
                decrypted_tenant_key = _unwrap_tenant_key(tenant_key, tenant.id, db=db)
                setattr(db_secret, "encrypted_value", encryption.encrypt_value(value, decrypted_tenant_key))
        else:
            setattr(db_secret, key, value)

    # If name changed, copy Vault secret to new name and delete old (best-effort delete).
    if "name" in update_data and update_data.get("name") and update_data.get("name") != old_name:
        try:
            v = _vault_get_tenant_secret(db_secret.tenant_id, old_name)
            if v is not None:
                _vault_put_tenant_secret(db_secret.tenant_id, db_secret.name, v)
                _vault_delete_tenant_secret(db_secret.tenant_id, old_name)
        except VaultError as exc:
            logger.warning(
                "Vault rename failed for tenant %s secret '%s' -> '%s': %s",
                db_secret.tenant_id, old_name, db_secret.name, exc,
            )

    db.add(db_secret)
    db.commit()
    db.refresh(db_secret)
    return db_secret


def delete_secret(db: Session, db_secret: models.Secret) -> models.Secret:
    """Soft deletes a secret by setting its 'deleted_at' timestamp."""
    db_secret.deleted_at = datetime.utcnow()
    # Best-effort Vault delete (don't block delete if Vault is temporarily down)
    try:
        _vault_delete_tenant_secret(db_secret.tenant_id, db_secret.name)
    except VaultError as exc:
        logger.warning(
            "Vault delete failed for tenant %s secret '%s' -- orphaned in Vault: %s",
            db_secret.tenant_id, db_secret.name, exc,
        )
    db.add(db_secret)
    db.commit()
    db.refresh(db_secret)
    return db_secret


def create_or_update_secret(
    db: Session,
    secret_name: str,
    value: str,
    user: models.User,
    description: str = ""
) -> models.Secret:
    """
    Create a new secret or update an existing one (restores soft-deleted if exists).
    Uses the tenant's encryption key for secure storage.
    """
    # 1. Retrieve (or backfill) the tenant's encrypted key
    tenant = user.tenant
    tenant_encryption_key = _ensure_tenant_encryption_key(db, tenant)

    # 2. Unwrap the tenant's key (local Fernet or Vault transit, per the
    #    envelope's own format — see services.key_provider)
    decrypted_tenant_key = _unwrap_tenant_key(tenant_encryption_key, tenant.id, db=db)

    # 3. Encrypt the new secret's value with the decrypted tenant key
    encrypted_value = encryption.encrypt_value(value, decrypted_tenant_key)

    # Store plaintext in Vault (fail closed)
    _vault_put_tenant_secret(user.tenant_id, secret_name, value)

    # 4. Look for ANY existing secret with this (name, tenant) — live or
    #    soft-deleted. Without including the live branch the function
    #    creates a brand-new row every time the caller resaves, so repeated
    #    Save clicks on e.g. the Integrations → Email page pile up duplicate
    #    smtp_password rows and the tenant ends up with 3+ live copies of
    #    the same secret, only the newest of which is canonical.
    existing = (
        db.query(models.Secret)
        .filter(
            models.Secret.name == secret_name,
            models.Secret.tenant_id == user.tenant_id,
        )
        .first()
    )

    if existing is not None:
        existing.encrypted_value = encrypted_value
        existing.description = description
        existing.deleted_at = None  # restores if it was soft-deleted
        existing.created_by_user_id = user.id
        db.commit()
        db.refresh(existing)
        return existing

    # 5. Create new secret — only when no row exists at all.
    db_secret = models.Secret()
    db_secret.name = secret_name
    db_secret.description = description
    db_secret.encrypted_value = encrypted_value
    db_secret.tenant_id = user.tenant_id
    db_secret.created_by_user_id = user.id
    db.add(db_secret)
    db.commit()
    db.refresh(db_secret)
    return db_secret
