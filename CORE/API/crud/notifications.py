"""CRUD data-access for the notifications domain (openspec: stack-training-and-prewarm, D9).

The ONLY place ``db.query(models.Notification)`` lives for this domain. The
notifications router (``routers/notifications.py``) and service
(``services/notifications_service.py``) reach for these functions instead of
hand-writing inline queries — so the scoping / filter / pagination discipline
lives in one place.

Scoping note: ``Notification`` is scoped by ``recipient_user_id`` (a user owns
their notifications); the model has no ``tenant_id`` or ``deleted_at`` column, so
the generic ``CRUDBase`` tenant/soft-delete filters do not apply. ``is_archived``
is the soft-hide flag (kept in the DB for audit, excluded from the default list).

Behaviour here is a straight lift of the previous inline router queries — it is
intentionally behaviour-preserving (Gate-2 migration, snapshot-guarded), NOT a
redesign. In particular the list total is the caller's UNFILTERED notification
count (as the original endpoint returned), not the count of the filtered slice.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

import models

# Chip label -> notification_type prefix union. The bell's filter chips map to
# this dict; a new category just adds an entry without touching any handler.
CATEGORY_TO_TYPE_PREFIXES = {
    "workflows": ["workflow_"],
    "nodes": ["node_"],
    "billing": ["payment_", "billing_", "invoice_", "overage"],
}


def user_scoped_query(db: Session, recipient_user_id: int):
    """Base query: every row this user owns. The single scoping chokepoint."""
    return db.query(models.Notification).filter(
        models.Notification.recipient_user_id == recipient_user_id
    )


def _apply_filters(
    query,
    *,
    unread_only: bool = False,
    include_archived: bool = False,
    category: Optional[str] = None,
):
    """Apply the /list filter knobs to a user-scoped query. Unknown category is
    a graceful no-op (matches the historical endpoint behaviour)."""
    if unread_only:
        query = query.filter(models.Notification.is_read == False)  # noqa: E712
    if not include_archived:
        query = query.filter(models.Notification.is_archived == False)  # noqa: E712
    if category:
        prefixes = CATEGORY_TO_TYPE_PREFIXES.get(category.lower())
        if prefixes:
            # notification_type is a free-form string that historically uses
            # prefix conventions (workflow_*, node_*, payment_*). Match by
            # OR-ing LIKE clauses.
            query = query.filter(
                or_(
                    *[
                        models.Notification.notification_type.ilike(f"{p}%")
                        for p in prefixes
                    ]
                )
            )
    return query


def filtered_query(
    db: Session,
    recipient_user_id: int,
    *,
    unread_only: bool = False,
    include_archived: bool = False,
    category: Optional[str] = None,
):
    """A user-scoped query with the /list filter knobs applied. Shared by the
    list read and the bulk archive/delete paths so they scope identically."""
    return _apply_filters(
        user_scoped_query(db, recipient_user_id),
        unread_only=unread_only,
        include_archived=include_archived,
        category=category,
    )


def list_for_user(
    db: Session,
    *,
    recipient_user_id: int,
    limit: int,
    offset: int,
    unread_only: bool = False,
    include_archived: bool = False,
    category: Optional[str] = None,
) -> Tuple[List[models.Notification], int]:
    """Return ``(rows, total)`` for a user's notifications, newest first.

    ``rows`` is the bounded, filtered slice. ``total`` preserves the original
    endpoint's semantics: the user's UNFILTERED notification count (not the
    count of the filtered window).
    """
    rows = (
        filtered_query(
            db,
            recipient_user_id,
            unread_only=unread_only,
            include_archived=include_archived,
            category=category,
        )
        .order_by(models.Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    total = user_scoped_query(db, recipient_user_id).count()
    return rows, total


def count_unread(db: Session, recipient_user_id: int) -> int:
    """Count of the user's unread notifications."""
    return (
        user_scoped_query(db, recipient_user_id)
        .filter(models.Notification.is_read == False)  # noqa: E712
        .count()
    )


def get_for_user(
    db: Session, notification_id: int, recipient_user_id: int
) -> Optional[models.Notification]:
    """Fetch one notification, scoped to its owner (never cross-user)."""
    return (
        user_scoped_query(db, recipient_user_id)
        .filter(models.Notification.id == notification_id)
        .first()
    )


def mark_all_read(db: Session, recipient_user_id: int) -> int:
    """Mark all of a user's unread notifications read. Returns the row count."""
    return (
        user_scoped_query(db, recipient_user_id)
        .filter(models.Notification.is_read == False)  # noqa: E712
        .update({"is_read": True})
    )


def archive_all(
    db: Session,
    recipient_user_id: int,
    *,
    unread_only: bool = False,
    include_archived: bool = False,
    category: Optional[str] = None,
) -> int:
    """Archive every currently-non-archived row matching the filter. Returns the
    row count. Archiving implies read."""
    base = filtered_query(
        db,
        recipient_user_id,
        unread_only=unread_only,
        include_archived=include_archived,
        category=category,
    )
    return base.filter(
        models.Notification.is_archived == False  # noqa: E712
    ).update(
        {"is_archived": True, "is_read": True},
        synchronize_session=False,
    )


def delete_all(
    db: Session,
    recipient_user_id: int,
    *,
    unread_only: bool = False,
    include_archived: bool = False,
    category: Optional[str] = None,
) -> int:
    """Soft-delete every row matching the filter. Returns the row count.

    iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    iso-no-hard-delete). ``Notification`` carries a ``deleted_at`` column
    (SoftDeleteMixin) and is in database.py's soft-delete listener, so a bulk
    UPDATE stamping ``deleted_at`` tombstones the rows and every live read
    (list/count/get above, all routed through ``user_scoped_query``) hides
    them exactly as a hard delete would.
    """
    base = filtered_query(
        db,
        recipient_user_id,
        unread_only=unread_only,
        include_archived=include_archived,
        category=category,
    )
    return base.update(
        {models.Notification.deleted_at: datetime.now(timezone.utc)},
        synchronize_session=False,
    )
