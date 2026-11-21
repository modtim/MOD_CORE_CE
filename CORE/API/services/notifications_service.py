"""Service layer for the notifications domain (openspec: stack-training-and-prewarm, D9).

Business logic / policy for notifications: the list max-page clamp, and the
owner-scoped single-row mutations (mark-read / archive / unarchive / delete).
Owns the transaction boundary; delegates ALL data access to
``crud/notifications.py``; carries no HTTP concerns (raises ``ServiceError``
subclasses, which the router maps to ``HTTPException`` — preserving the existing
404 responses exactly).

Behaviour-preserving Gate-2 migration: every method reproduces the previous
inline-router behaviour, just relocated. Note the category filter is a graceful
no-op for unknown values (NOT a validation error) — matching the historical
endpoint and its tests.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

import models
from crud import notifications as notifications_crud
from services._base import NotFoundError


class NotificationService:
    """Policy + transaction boundary for the notifications domain."""

    #: Hard cap on a single /list page, independent of any request value.
    MAX_PAGE = 100

    def list_for_user(
        self,
        db: Session,
        *,
        user_id: int,
        limit: int,
        offset: int,
        unread_only: bool = False,
        include_archived: bool = False,
        category: Optional[str] = None,
    ) -> Tuple[List[models.Notification], int]:
        """List a user's notifications, newest first. Clamps the page size to
        ``MAX_PAGE`` (the historical behaviour). Returns ``(rows, total)`` where
        ``total`` is the user's unfiltered notification count."""
        if limit > self.MAX_PAGE:
            limit = self.MAX_PAGE
        return notifications_crud.list_for_user(
            db,
            recipient_user_id=user_id,
            limit=limit,
            offset=offset,
            unread_only=unread_only,
            include_archived=include_archived,
            category=category,
        )

    def unread_count(self, db: Session, *, user_id: int) -> int:
        """Count of the user's unread notifications."""
        return notifications_crud.count_unread(db, user_id)

    def mark_read(
        self, db: Session, *, user_id: int, notification_id: int
    ) -> models.Notification:
        """Mark one of the caller's notifications read; raise if not theirs."""
        row = notifications_crud.get_for_user(db, notification_id, user_id)
        if row is None:
            raise NotFoundError("Notification not found")
        row.is_read = True
        db.commit()
        return row

    def mark_all_read(self, db: Session, *, user_id: int) -> int:
        """Mark all of the caller's notifications read. Returns the row count."""
        count = notifications_crud.mark_all_read(db, user_id)
        db.commit()
        return count

    def archive(
        self, db: Session, *, user_id: int, notification_id: int
    ) -> models.Notification:
        """Archive (soft-hide) one of the caller's notifications; imply read."""
        row = notifications_crud.get_for_user(db, notification_id, user_id)
        if row is None:
            raise NotFoundError("Notification not found")
        row.is_archived = True
        if not row.is_read:
            row.is_read = True  # archiving implies read
        db.commit()
        return row

    def unarchive(
        self, db: Session, *, user_id: int, notification_id: int
    ) -> models.Notification:
        """Undo archive — put the row back into the default list view."""
        row = notifications_crud.get_for_user(db, notification_id, user_id)
        if row is None:
            raise NotFoundError("Notification not found")
        row.is_archived = False
        db.commit()
        return row

    def delete(self, db: Session, *, user_id: int, notification_id: int) -> None:
        """Soft-delete one of the caller's notifications; raise if not theirs.

        ISO 27001 no-hard-delete (MEMORY iso-no-hard-delete): Notification
        carries SoftDeleteMixin and is in database.py's soft-delete listener,
        so stamp `deleted_at` and RETAIN the row — live reads drop the
        tombstone.
        """
        row = notifications_crud.get_for_user(db, notification_id, user_id)
        if row is None:
            raise NotFoundError("Notification not found")
        from datetime import datetime as _dt, timezone as _tz
        row.deleted_at = _dt.now(_tz.utc)
        db.commit()

    def archive_all(
        self,
        db: Session,
        *,
        user_id: int,
        unread_only: bool = False,
        include_archived: bool = False,
        category: Optional[str] = None,
    ) -> int:
        """Archive every notification matching the filter. Returns the count."""
        count = notifications_crud.archive_all(
            db,
            user_id,
            unread_only=unread_only,
            include_archived=include_archived,
            category=category,
        )
        db.commit()
        return count

    def delete_all(
        self,
        db: Session,
        *,
        user_id: int,
        unread_only: bool = False,
        include_archived: bool = False,
        category: Optional[str] = None,
    ) -> int:
        """Hard-delete every notification matching the filter. Returns the count."""
        count = notifications_crud.delete_all(
            db,
            user_id,
            unread_only=unread_only,
            include_archived=include_archived,
            category=category,
        )
        db.commit()
        return count


notification_service = NotificationService()
