"""Shareable Link CRUD operations - Shareable link management within tenants."""
from typing import Optional, List
from datetime import datetime
import hmac
import logging

from sqlalchemy.orm import Session
import models, schemas
import secrets

import bcrypt as _bcrypt

logger = logging.getLogger(__name__)

# R-095/R-110 — the gate password was stored as `f"hashed_{password}"`
# (plaintext at rest behind a fake prefix). It is now bcrypt, in the same
# `password_hash` column — no schema change. bcrypt is used directly (the
# passlib 1.7.4 + bcrypt 4.x combo is broken in this environment);
# `security.hash_api_key` proves the pattern. Legacy `hashed_<plaintext>`
# rows are accepted once by the verify path below and re-hashed in place to
# bcrypt on first successful use; the remaining rows are swept by
# migrations/20261004i_shareable_link_bcrypt.py.
LEGACY_HASHED_PREFIX = "hashed_"


def _truncate_for_bcrypt(plain: str) -> str:
    # bcrypt caps input at 72 bytes; truncate the same way the rest of the
    # codebase does (security.hash_api_key) so long passwords behave
    # identically at hash- and verify-time.
    b = plain.encode("utf-8")
    return b[:72].decode("utf-8", errors="ignore")


def hash_link_password(plain: str) -> str:
    """Bcrypt a shareable-link gate password."""
    return _bcrypt.hashpw(
        _truncate_for_bcrypt(plain).encode("utf-8"), _bcrypt.gensalt()
    ).decode("utf-8")


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_link_password(stored_hash: str, plain: str) -> bool:
    """Verify *plain* against a stored gate password.

    Bcrypt rows verify through bcrypt; legacy `hashed_<plaintext>` rows are
    compared in constant time against the stripped value (they will be
    re-hashed by the caller on success).
    """
    if not stored_hash or not plain:
        return False
    if stored_hash.startswith(LEGACY_HASHED_PREFIX):
        return _constant_time_equal(
            _truncate_for_bcrypt(plain),
            _truncate_for_bcrypt(stored_hash[len(LEGACY_HASHED_PREFIX):]),
        )
    try:
        return bool(_bcrypt.checkpw(
            _truncate_for_bcrypt(plain).encode("utf-8"),
            stored_hash.encode("utf-8"),
        ))
    except Exception:  # noqa: BLE001 — malformed/corrupt hash is a non-match
        return False


def verify_shareable_link_password(
    db: Session,
    link: models.ShareableLink,
    provided: Optional[str]
) -> bool:
    """Verify *provided* against the link's stored gate password.

    A legacy `hashed_<plaintext>` row that matches is re-hashed in place to
    bcrypt (UPDATE, never delete) so it is only accepted in its legacy form
    once. Returns False for links without a password (callers treat those as
    unprotected) or for non-matching passwords.
    """
    if not link.password_hash or not provided:
        return False
    if not verify_link_password(link.password_hash, provided):
        return False
    if link.password_hash.startswith(LEGACY_HASHED_PREFIX):
        link.password_hash = hash_link_password(provided)
        db.add(link)
        db.commit()
    return True


def soft_delete_shareable_link(db: Session, db_link: models.ShareableLink) -> models.ShareableLink:
    """Soft deletes a shareable link by setting the 'deleted_at' field."""
    db_link.deleted_at = datetime.utcnow()
    db.add(db_link)
    db.commit()
    db.refresh(db_link)
    return db_link


def get_shareable_link_for_owner(
    db: Session,
    link_id: int,
    owner_id: int,
    tenant_id: int
) -> Optional[models.ShareableLink]:
    """
    Retrieves a single active shareable link by its ID,
    ensuring it belongs to a cog owned by the specified owner within the tenant.
    """
    return (
        db.query(models.ShareableLink)
        .join(models.Cog, models.ShareableLink.cog_id == models.Cog.id)
        .filter(
            models.ShareableLink.id == link_id,
            models.ShareableLink.tenant_id == tenant_id,
            models.Cog.owner_id == owner_id,
            models.ShareableLink.deleted_at.is_(None)
        )
        .first()
    )


def create_shareable_link(
    db: Session,
    cog_id: int,
    user_id: int,
    tenant_id: int,
    link_create: schemas.ShareableLinkCreate
) -> models.ShareableLink:
    """Creates a new shareable link for a cog in the database."""
    token = secrets.token_urlsafe(32)

    password_hash = None
    if link_create.password:
        password_hash = hash_link_password(link_create.password)  # R-095: bcrypt, not plaintext

    # Coerce the incoming string ("viewer"/"editor"/"owner") into the
    # PermissionLevel enum. Postgres enum labels are uppercase; without
    # this the ORM sends the raw lowercase string and Postgres rejects it.
    level = link_create.permission_level
    if isinstance(level, str):
        level = models.PermissionLevel(level)

    db_link = models.ShareableLink()
    db_link.tenant_id = tenant_id
    db_link.cog_id = cog_id
    db_link.created_by_user_id = user_id
    db_link.token = token
    db_link.permission_level = level
    db_link.expires_at = link_create.expires_at
    db_link.password_hash = password_hash
    db.add(db_link)
    db.commit()
    db.refresh(db_link)
    return db_link


def get_shareable_links_for_cog(db: Session, cog_id: int, tenant_id: int) -> List[models.ShareableLink]:
    """Retrieves all active shareable links for a specific cog within a tenant."""
    return (
        db.query(models.ShareableLink)
        .filter(
            models.ShareableLink.cog_id == cog_id,
            models.ShareableLink.tenant_id == tenant_id,
            models.ShareableLink.deleted_at.is_(None)
        )
        .all()
    )


def get_active_link_by_token(db: Session, token: str) -> Optional[models.ShareableLink]:
    """Resolves a live (non-revoked) share link by its public token.

    FX.102 — the token is the ONLY credential a visitor holds; the
    tenant is carried by the row itself (never taken from the request).
    Returns None for unknown OR soft-deleted tokens so a probe cannot
    distinguish the two (the visit path answers the same 404).
    """
    if not token:
        return None
    return (
        db.query(models.ShareableLink)
        .filter(
            models.ShareableLink.token == token,
            models.ShareableLink.deleted_at.is_(None),
        )
        .first()
    )


def revoke_all_active_links(db: Session, tenant_id: int) -> List[models.ShareableLink]:
    """FX.102 — soft-revoke EVERY active share link in a tenant
    (tenant admin 'revoke all'). Soft-delete only (ISO: no hard
    delete): deleted_at is stamped, rows remain for the audit trail.
    Returns the revoked links (for the caller's audit rows)."""
    now = datetime.utcnow()
    links = (
        db.query(models.ShareableLink)
        .filter(
            models.ShareableLink.tenant_id == tenant_id,
            models.ShareableLink.deleted_at.is_(None),
        )
        .all()
    )
    for link in links:
        link.deleted_at = now
        db.add(link)
    if links:
        db.commit()
    return links
