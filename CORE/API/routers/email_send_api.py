"""GX.S4 (launch-completion-megaplan Group X) — general email send API.

Routes:
    POST /v1/email/send                send via the tenant's SMTP (CE)
    GET  /v1/email/messages            list this tenant's records (CE)
    GET  /v1/email/messages/{id}       fetch one record (404 cross-tenant)

Auth: `require_bearer_or_api_key` (Bearer token OR scoped app key) —
tenant-scoped by the authenticated user's tenant. Business logic + the
CE/Paid edition seam live in services/email_send_api.py.

Records are METADATA ONLY (recipient lists, sizes, attachment names/types/
sizes) — attachment bytes and bodies are never persisted.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_bearer_or_api_key
from utils.http_errors import internal_http_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email", tags=["Email Send API"])

_MAX_PAGE = 100


class EmailAttachmentIn(BaseModel):
    """One attachment (base64-encoded bytes; metadata only is persisted)."""
    filename: str = Field(..., min_length=1, max_length=255)
    mime_type: str = Field("application/octet-stream", max_length=255)
    content: str = Field("", description="base64-encoded attachment bytes")


class EmailSendRequest(BaseModel):
    to: List[str] = Field(default_factory=list)
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)
    subject: str
    text: Optional[str] = None
    html: Optional[str] = None
    attachments: List[EmailAttachmentIn] = Field(default_factory=list)


class EmailMessageOut(BaseModel):
    id: int
    app_id: Optional[int] = None
    sender_user_id: Optional[int] = None
    to: List[str]
    cc: List[str]
    bcc: List[str]
    subject: str
    size_bytes: int
    attachments: List[dict] = Field(default_factory=list)
    status: str
    error: Optional[str] = None
    provider_message_id: Optional[str] = None
    created_at: Optional[str] = None
    sent_at: Optional[str] = None


class EmailSendOut(EmailMessageOut):
    pass


class EmailMessageListOut(BaseModel):
    items: List[EmailMessageOut]
    total: int
    skip: int
    limit: int


def _parse_attachments(items: List[EmailAttachmentIn]) -> List[dict]:
    import base64
    out = []
    for a in items:
        try:
            raw = base64.b64decode(a.content, validate=True) if a.content else b""
        except Exception as e:  # noqa: BLE001
            raise internal_http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Attachment content is not valid base64", e) from e
        out.append({
            "filename": a.filename,
            "mime_type": a.mime_type,
            "_bytes": raw,  # in-memory only; stripped before the record
        })
    return out


@router.post("/send", status_code=status.HTTP_202_ACCEPTED,
             response_model=EmailSendOut)
def send_email(
    payload: EmailSendRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """Send an email via the tenant's configured SMTP.

    Returns 202 with the queued record (id + status); the send runs off the
    request thread and the record flips to sent/failed. 409 when the tenant
    has no SMTP config (no record created), 413/422/429 on limit breaches.
    """
    from services import email_send_api
    row = email_send_api.prepare_send(
        db,
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        app_id=None,  # GX.A2 adds the apps axis
        to=payload.to,
        cc=payload.cc,
        bcc=payload.bcc,
        subject=payload.subject,
        text=payload.text,
        html=payload.html,
        attachments=_parse_attachments(payload.attachments),
    )
    return email_send_api.message_public(row)


@router.get("/messages", response_model=EmailMessageListOut)
def list_messages(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=_MAX_PAGE),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """This tenant's email records, newest first (paginated)."""
    from services import email_send_api
    items, total = email_send_api.list_messages(
        db, current_user.tenant_id, skip=skip, limit=limit,
        status=status_filter,
    )
    return {
        "items": [email_send_api.message_public(r) for r in items],
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get("/messages/{message_id}", response_model=EmailMessageOut)
def get_message(
    message_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_bearer_or_api_key),
):
    """One record from this tenant (404 for missing OR cross-tenant)."""
    from services import email_send_api
    row = email_send_api.get_message(db, message_id, current_user.tenant_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Email message not found.")
    return email_send_api.message_public(row)
