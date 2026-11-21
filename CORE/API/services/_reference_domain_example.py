"""WORKED REFERENCE: the notifications domain, done the canonical way.

TEMPLATE / EXAMPLE ONLY — this module is NOT imported by main.py and the real
`routers/notifications.py` is left untouched. It exists so a Gate-2 domain-agent
can copy the shape rather than invent it.

It re-implements the existing `GET /notifications/list` endpoint
(routers/notifications.py:128), which today is the textbook anti-pattern the
audit flagged: the router runs TWO inline `db.query(models.Notification)` calls,
hand-clamps the limit, hand-rolls offset/limit, and returns a bare dict. Here the
same behaviour is expressed as:

    router (this file, `example_router`)   -- HTTP + auth + Page envelope only
        -> NotificationService            -- clamp/policy + category rules
            -> NotificationCRUD           -- the ONLY db.query for this domain
                -> models.Notification

Compare `list_notifications` below (thin) with the real handler (fat) to see
what the refactor moves and why.

The four canonical conventions in use:
    * auth        : `api_conventions.access(...)` -> AccessContext(user, tenant)
    * pagination  : `api_conventions.pagination` -> Pagination -> Page envelope
    * data access : `NotificationCRUD` / `services._base.CRUDBase` (no inline
                    db.query in the router)
    * errors      : service raises `ServiceError`; router maps to HTTPException
                    (in the real adoption the installed handler renders the
                    canonical `ErrorEnvelope`).
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_
from sqlalchemy.orm import Session

import models
from api_conventions import AccessContext, Page, Pagination, access, pagination
from dependencies import get_db
from services._base import CRUDBase, NotFoundError, ServiceError


# =============================================================================
# Response schema (would normally live in schemas.py / the domain's schemas)
# =============================================================================


class NotificationOut(BaseModel):
    """Canonical output schema — every endpoint declares a `response_model`
    (fixes audit §7: 614 endpoints with none)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    message: Optional[str] = None
    is_read: bool
    notification_type: Optional[str] = None
    link_url: Optional[str] = None
    is_archived: bool


# =============================================================================
# CRUD layer — the ONLY place db.query(models.Notification) lives (this domain)
# =============================================================================


# Chip label -> notification_type prefix union (moved out of the router body).
_CATEGORY_TO_TYPE_PREFIXES = {
    "workflows": ["workflow_"],
    "nodes": ["node_"],
    "billing": ["payment_", "billing_", "invoice_", "overage"],
}


class NotificationCRUD(CRUDBase[models.Notification]):
    """Data access for notifications. All filtering/pagination discipline lives
    here once, instead of being re-hand-written in the router."""

    default_order_by = None  # custom ordering below (created_at DESC)

    def list_for_user(
        self,
        db: Session,
        *,
        recipient_user_id: int,
        limit: int,
        offset: int,
        unread_only: bool = False,
        include_archived: bool = False,
        category: Optional[str] = None,
    ) -> Tuple[List[models.Notification], int]:
        """Return `(rows, total)` for a user's notifications, newest first.

        Bounded by construction (the caller passes a validated limit/offset from
        `api_conventions.Pagination`). The total is computed on the same
        filtered query so pagination metadata is correct.
        """
        query = db.query(models.Notification).filter(
            models.Notification.recipient_user_id == recipient_user_id
        )
        if unread_only:
            query = query.filter(models.Notification.is_read.is_(False))
        if not include_archived:
            query = query.filter(models.Notification.is_archived.is_(False))
        if category:
            prefixes = _CATEGORY_TO_TYPE_PREFIXES.get(category.lower())
            if prefixes:
                query = query.filter(
                    or_(
                        *[
                            models.Notification.notification_type.ilike(f"{p}%")
                            for p in prefixes
                        ]
                    )
                )

        total = query.order_by(None).count()
        rows = (
            query.order_by(models.Notification.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return rows, total

    def get_for_user(
        self, db: Session, notification_id: int, *, recipient_user_id: int
    ) -> Optional[models.Notification]:
        """Fetch one notification, scoped to its owner (never cross-user)."""
        return (
            db.query(models.Notification)
            .filter(
                models.Notification.id == notification_id,
                models.Notification.recipient_user_id == recipient_user_id,
            )
            .first()
        )


notifications_crud = NotificationCRUD(models.Notification)


# =============================================================================
# Service layer — policy / business rules, no HTTP, no bare db.query
# =============================================================================


class NotificationService:
    """Business logic for the notifications domain. Owns policy (the max-limit
    clamp, category validation) and the transaction boundary; delegates all data
    access to the CRUD layer. Reusable by a background job, not just HTTP."""

    #: Policy cap independent of the API's generic pagination bound.
    MAX_PAGE = 100

    def __init__(self, crud: NotificationCRUD = notifications_crud):
        self._crud = crud

    def list_for_user(
        self,
        db: Session,
        *,
        user_id: int,
        page: Pagination,
        unread_only: bool = False,
        include_archived: bool = False,
        category: Optional[str] = None,
    ) -> Tuple[List[models.Notification], int]:
        """List a user's notifications with domain policy applied."""
        if category and category.lower() not in _CATEGORY_TO_TYPE_PREFIXES:
            raise ServiceError(
                f"Unknown category '{category}'.",
                code="bad_request",
                details={"allowed": sorted(_CATEGORY_TO_TYPE_PREFIXES)},
            )
        limit = min(page.limit, self.MAX_PAGE)
        return self._crud.list_for_user(
            db,
            recipient_user_id=user_id,
            limit=limit,
            offset=page.offset,
            unread_only=unread_only,
            include_archived=include_archived,
            category=category,
        )

    def mark_read(self, db: Session, *, user_id: int, notification_id: int) -> models.Notification:
        """Mark one of the caller's notifications read; raise if it isn't theirs."""
        row = self._crud.get_for_user(db, notification_id, recipient_user_id=user_id)
        if row is None:
            raise NotFoundError("Notification not found.")
        row.is_read = True
        db.commit()
        db.refresh(row)
        return row


notification_service = NotificationService()


# =============================================================================
# Router layer — HTTP only. NO db.query here.
# =============================================================================

# NOTE: `example_router` is intentionally NOT included in main.py. It documents
# the target handler shape; it does not register real routes.
example_router = APIRouter(tags=["_reference_example"])


def _raise_http(exc: ServiceError) -> None:
    """Map a service-layer error to HTTP. In full adoption the installed
    exception handler renders the canonical `ErrorEnvelope`; here we map inline
    so the example is self-contained."""
    status = {"not_found": 404, "conflict": 409, "bad_request": 400}.get(exc.code, 400)
    raise HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message, "details": exc.details})


@example_router.get("/notifications/list", response_model=Page[NotificationOut])
def list_notifications(
    page: Pagination = Depends(pagination),
    unread_only: bool = False,
    include_archived: bool = False,
    category: Optional[str] = None,
    ctx: AccessContext = Depends(access()),
    db: Session = Depends(get_db),
) -> Page[NotificationOut]:
    """Canonical list handler: auth via `access()`, bounded via `pagination`,
    data via the service, response via the `Page` envelope. Contrast with the
    real fat handler at routers/notifications.py:128."""
    try:
        rows, total = notification_service.list_for_user(
            db,
            user_id=ctx.user_id,
            page=page,
            unread_only=unread_only,
            include_archived=include_archived,
            category=category,
        )
    except ServiceError as exc:
        _raise_http(exc)
    items = [NotificationOut.model_validate(r) for r in rows]
    return Page.build(items, total, page)


@example_router.put("/notifications/{notification_id}/mark-read", response_model=NotificationOut)
def mark_notification_read(
    notification_id: int,
    ctx: AccessContext = Depends(access()),
    db: Session = Depends(get_db),
) -> NotificationOut:
    """Canonical write handler: owner-scoped, service-mediated, typed response."""
    try:
        row = notification_service.mark_read(db, user_id=ctx.user_id, notification_id=notification_id)
    except ServiceError as exc:
        _raise_http(exc)
    return NotificationOut.model_validate(row)
