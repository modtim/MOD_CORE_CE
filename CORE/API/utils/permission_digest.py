"""Permission-digest queue inserter.

Replaces the immediate per-event email sends for `workflow_complete`
and `permission_expiration` notifications. Call sites that used to
invoke `send_permission_notification_email` / `send_permission_expiration_email`
directly now INSERT a row here instead; the
services.permission_digest scheduler job drains rows once per user
per day in the user-local 09:00 window and ships one consolidated
digest.

Time-sensitive emails — billing (invoice / payment_retry /
grace_period), permission_request, invitations, welcome — still send
immediately. This module only handles the spammy "FYI" categories.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)


# Event types this queue accepts. Keeping the set explicit so a typo
# in a caller can't silently route a billing email into the digest.
DIGEST_EVENT_TYPES = frozenset({
    "workflow_complete",
    "permission_expiration",
})


def queue_digest_event(
    db: Session,
    user_id: int,
    tenant_id: int,
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
) -> bool:
    """Append one digest event for *user_id*.

    Returns True on successful INSERT, False on any failure (logged,
    not raised — the caller is usually a notification path that
    shouldn't fail its parent action because the queue write blipped).

    The row sits with sent_at IS NULL until the next 09:00 user-local
    pass picks it up. There's intentionally no de-dup here: if a user
    completes 30 workflows in a day they'll see 30 entries in the
    digest body, which is the point — they get the same coverage as
    before, just consolidated.
    """
    if event_type not in DIGEST_EVENT_TYPES:
        logger.warning(
            "queue_digest_event called with unrecognised event_type=%r (user=%s); "
            "falling back to immediate send is the caller's responsibility",
            event_type, user_id,
        )
        return False

    try:
        row = models.PendingPermissionEmail(
            user_id=user_id,
            tenant_id=tenant_id,
            event_type=event_type,
            event_payload=payload or {},
        )
        db.add(row)
        db.commit()
        return True
    except Exception as e:
        logger.warning(
            "queue_digest_event INSERT failed (user=%s type=%s): %s",
            user_id, event_type, e,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return False
