"""API Key CRUD operations - API key management for users."""
from typing import Optional, List

from sqlalchemy.orm import Session
from datetime import datetime

import models, schemas
from utils.encryption import encrypt_value
from utils.paid_import import optional_paid

# GSEC/1634c — services/api_key_format is manifest-denied (Paid-only), so a
# module-level `from services import api_key_format` crashes CE at boot before
# any request is served. Route it through utils/paid_import.optional_paid; the
# CE fallback below is the legacy rule (first 8 characters of the key), which
# is exactly what lookup_prefixes returns for a value it cannot parse.
api_key_format = optional_paid("services.api_key_format")


def create_api_key(
    db: Session,
    *,
    user_id: int,
    key_prefix: str,
    hashed_key: str,
    description: str,
    expires_at: datetime
) -> models.ApiKey:
    """
    Saves a new API key record to the database.
    """
    db_api_key = models.ApiKey()
    db_api_key.user_id = user_id
    db_api_key.key_prefix = key_prefix
    db_api_key.hashed_key = hashed_key
    db_api_key.description = description
    db_api_key.expires_at = expires_at

    db.add(db_api_key)
    db.commit()
    db.refresh(db_api_key)

    return db_api_key


def get_api_key_by_key_prefix(db: Session, key_prefix: str) -> Optional[models.ApiKey]:
    """Retrieve an API key record by its key prefix.

    GSEC FX.152 part 1 (Q139=B): `key_prefix` may be a full key or an 8-char
    prefix. Legacy `sk_...` keys stored the first 8 characters of the key, so
    that candidate is unchanged; new `mod_live_`/`mod_test_` keys store the
    first 8 characters of the random body (the first 8 characters of the key
    are always the same literal and would collide on the unique column), so
    both candidates are tried. Nothing here assumes the `sk_` prefix.
    """
    if api_key_format is None:
        # CE: no format parser -> the legacy rule only (first 8 characters),
        # which is what lookup_prefixes returns for a value it cannot parse.
        candidates = (key_prefix[:8],) if key_prefix else ()
    else:
        candidates = api_key_format.lookup_prefixes(key_prefix)
    if not candidates:
        return None
    return (
        db.query(models.ApiKey)
        .filter(models.ApiKey.key_prefix.in_(candidates), models.ApiKey.deleted_at.is_(None))
        .first()
    )


def get_api_key_by_keycloak_client_id(db: Session, keycloak_client_id: str) -> Optional[models.ApiKey]:
    """
    Retrieves an API key record by its Keycloak client ID.

    Used during API key token authentication to map the service account
    back to the original user who created the API key.
    """
    return (
        db.query(models.ApiKey)
        .filter(
            models.ApiKey.keycloak_client_id == keycloak_client_id,
            models.ApiKey.deleted_at.is_(None)
        )
        .first()
    )


def get_api_keys_for_user(
    db: Session,
    *,
    user_id: int,
    skip: int = 0,
    limit: int = 100
) -> List[models.ApiKey]:
    """Retrieve active API keys for a specific user with pagination."""
    return (
        db.query(models.ApiKey)
        .filter(models.ApiKey.user_id == user_id, models.ApiKey.deleted_at.is_(None))
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_user_api_key(db: Session, *, api_key_id: int, user_id: int) -> Optional[models.ApiKey]:
    """
    Retrieves a single active API key by its ID, ensuring it belongs to the specified user.
    """
    return (
        db.query(models.ApiKey)
        .filter(
            models.ApiKey.id == api_key_id,
            models.ApiKey.user_id == user_id,
            models.ApiKey.deleted_at.is_(None)
        )
        .first()
    )


def revoke_api_key(db: Session, *, db_api_key: models.ApiKey) -> models.ApiKey:
    """
    Revokes an API key by setting its 'deleted_at' field (soft delete).
    """
    db_api_key.deleted_at = datetime.utcnow()
    db.add(db_api_key)
    db.commit()
    db.refresh(db_api_key)
    return db_api_key
