"""GX.S6 (launch-completion-megaplan Group X) — per-stack/app storage scopes.

Routes (auth: ``require_bearer_or_api_key`` — Bearer token OR scoped app key):
    POST   /v1/storage/scopes/{kind}/{scope_id}/links
           issue a presigned PUT or GET URL for a key inside the scope
           prefix (``stacks/{id}/`` or ``apps/{id}/`` in the tenant's file
           bucket, or its BYO-S3 bucket).
    GET    /v1/storage/scopes/{kind}/{scope_id}/objects?prefix=
           paginated listing of the scope's live objects (keys relative to
           the scope prefix; ``.trash/`` entries are never listed).
    DELETE /v1/storage/scopes/{kind}/{scope_id}/objects/{key}
           SOFT delete: tombstone row + bytes copied to the scope's
           ``.trash/`` prefix (ISO 27001 — no hard deletes).

Business logic lives in services/storage_scopes.py.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_bearer_or_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/storage/scopes", tags=["Storage Scopes"])


# ---------------------------------------------------------------------------
# request / response models
# ---------------------------------------------------------------------------

class LinkRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=512,
                     description="Scope-relative object key (no leading /, "
                                 "no '..' segments).")
    method: str = Field(..., description="'GET' or 'PUT'")
    expires_in: Optional[int] = Field(
        None, ge=1,
        description="TTL in seconds; capped at the "
                    "storage_scopes.link_max_ttl_seconds setting.",
    )
    content_type: Optional[str] = Field(
        None, max_length=255,
        description="Optional Content-Type for PUT links.",
    )


class LinkOut(BaseModel):
    url: str
    bucket: str
    key: str
    method: str
    full_key: str
    expires_in: int
    max_size_bytes: Optional[int] = None
    content_type: Optional[str] = None


class ObjectItem(BaseModel):
    key: str
    size_bytes: int
    last_modified: Optional[str] = None


class ObjectListOut(BaseModel):
    items: list[ObjectItem]
    prefix: Optional[str] = None
    skip: int
    limit: int
    has_more: bool


class DeleteObjectOut(BaseModel):
    key: str
    status: str
    trash_key: str
    deleted_at: str
    copy_error: Optional[str] = None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@router.post("/{kind}/{scope_id}/links", response_model=LinkOut)
def create_link(
    kind: str,
    scope_id: int,
    payload: LinkRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Issue a presigned PUT or GET URL for ``payload.key`` inside the
    scope. The key is validated and joined with the scope prefix so the
    signed URL can never address anything outside the scope. TTL is
    capped at the platform setting."""
    from services import storage_scopes
    return storage_scopes.create_link(
        db,
        tenant_id=current_user.tenant_id,
        kind=kind,
        scope_id=scope_id,
        key=payload.key,
        method=payload.method,
        expires_in=payload.expires_in,
        content_type=payload.content_type,
        user=current_user,
    )


@router.get("/{kind}/{scope_id}/objects", response_model=ObjectListOut)
def list_objects(
    kind: str,
    scope_id: int,
    prefix: Optional[str] = Query(
        None, max_length=512,
        description="Scope-relative sub-prefix filter."),
    skip: int = Query(0, ge=0),
    limit: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Paginated listing of the scope's live objects (keys are relative to
    the scope prefix). Objects in the ``.trash/`` prefix are never
    listed. ``limit`` is capped at the ``storage_scopes.list_page_size``
    setting."""
    from services import storage_scopes
    return storage_scopes.list_objects(
        db,
        tenant_id=current_user.tenant_id,
        kind=kind,
        scope_id=scope_id,
        prefix=prefix,
        skip=skip,
        limit=limit,
        user=current_user,
    )


@router.delete("/{kind}/{scope_id}/objects/{key:path}",
               response_model=DeleteObjectOut)
def delete_object(
    kind: str,
    scope_id: int,
    key: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Soft-delete one scope object (ISO 27001 — no hard deletes): a
    tombstone row is written and the bytes are moved to the scope's
    ``.trash/`` prefix rather than destroyed."""
    from services import storage_scopes
    return storage_scopes.soft_delete_object(
        db,
        tenant_id=current_user.tenant_id,
        kind=kind,
        scope_id=scope_id,
        key=key,
        user=current_user,
    )
