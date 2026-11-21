"""HF token CRUD — thin wrapper over the existing tenant-secret CRUD (GF.C0b.2).

An HF token is a plain tenant Secret named `HF_TOKEN_SECRET_NAME`
(services/secrets_service); nothing here knows HuggingFace-specific things
beyond the name. All reads are strictly tenant-scoped: the tenant_id is
always a parameter, never taken from a request.
"""
from typing import Optional

from sqlalchemy.orm import Session

import models
from crud import secret_cud
from services.secrets_service import HF_TOKEN_SECRET_NAME


def get_secret(db: Session, tenant_id: int) -> Optional[models.Secret]:
    """The tenant's live HF_TOKEN secret row, or None if not set (or soft-deleted)."""
    return secret_cud.get_secret_by_name(
        db, secret_name=HF_TOKEN_SECRET_NAME, tenant_id=tenant_id
    )


def get_decrypted_value(db: Session, tenant_id: int) -> Optional[str]:
    """The tenant's own HF token decrypted from Vault, or None. Never logs it."""
    return secret_cud.get_decrypted_secret_by_name(
        db, secret_name=HF_TOKEN_SECRET_NAME, tenant_id=tenant_id
    )


def create_or_rotate(
    db: Session, tenant_id: int, user: models.User, value: str
) -> models.Secret:
    """Create (or rotate) the tenant's HF token.

    Uses secret_cud.create_or_update_secret — the true upsert — so a second
    call ROTATES the existing live row in place (single row, value refreshed)
    and restores it if it had been soft-deleted. Plain secret_cud.create_secret
    only restores soft-deleted rows, so re-saver while live would pile up a
    second live "HF_TOKEN" row. Same Fernet + per-tenant-key + Vault dual-write
    path as every other tenant secret; ISO: no hard delete.
    """
    return secret_cud.create_or_update_secret(
        db,
        secret_name=HF_TOKEN_SECRET_NAME,
        value=value,
        user=user,
    )


def soft_delete(db: Session, db_secret: models.Secret) -> models.Secret:
    """Soft-delete via the Secret's existing path (sets deleted_at, best-effort
    Vault delete). Never hard-deletes — ISO 27001."""
    return secret_cud.delete_secret(db, db_secret=db_secret)
