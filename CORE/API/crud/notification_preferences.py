"""CRUD data-access for per-user notification preferences (WS4.Q22).

A preference is keyed by (user_id, category, channel). ``category`` is the
UI-facing grouping the account-settings page shows toggles for — NOT the raw
``Notification.notification_type`` string. ``category_for_type`` maps a raw
type (e.g. ``"workflow_complete"``) to its category (``"workflow"``); a type
with no known category (e.g. ``"project_question"``) has no user-facing
toggle and is always treated as enabled.

Missing row == enabled. This module never treats "no row" as "disabled" —
every read function returns True (or omits the category, per the caller's
choice) when nothing was ever stored, matching "defaults to enabled" from
the spec.
"""
from __future__ import annotations

from typing import Dict, Optional

from sqlalchemy.orm import Session

import models

# The three categories the account-settings notification-preferences card
# exposes toggles for (CORE/Dashboard/pages/account.html, "Notification
# preferences" card). Add a category here + a UI toggle together.
CATEGORIES = ("workflow", "billing", "product")

CHANNELS = ("in_app", "email")

# Raw Notification.notification_type -> preference category. Keep in sync
# with every `notification_type=` literal used when a Notification row is
# created (grep models.py / routers / services for `notification_type=`).
TYPE_TO_CATEGORY: Dict[str, str] = {
    "workflow_complete": "workflow",
    "workflow_failed": "workflow",
    "payment_failed": "billing",
    "payment_success": "billing",
    "billing_failure": "billing",
    "grace_period_warning": "billing",
    "account_suspended": "billing",
    "account_reactivated": "billing",
    "subscription_downgraded": "billing",
    "release_published": "product",
}

# Prefix fallback for types not in the explicit map above (new types keep
# working without a code change as long as they follow the existing naming
# convention).
_CATEGORY_PREFIXES = (
    ("workflow", ("workflow_",)),
    ("billing", ("payment_", "billing_", "invoice_", "overage")),
    ("product", ("release_", "product_")),
)


def category_for_type(notification_type: Optional[str]) -> Optional[str]:
    """Resolve a raw ``notification_type`` to its preference category, or
    ``None`` if it has no user-facing toggle (always enabled in that case)."""
    if not notification_type:
        return None
    cat = TYPE_TO_CATEGORY.get(notification_type)
    if cat:
        return cat
    for cat, prefixes in _CATEGORY_PREFIXES:
        if any(notification_type.startswith(p) for p in prefixes):
            return cat
    return None


def _query(db: Session, user_id: int):
    return db.query(models.NotificationPreference).filter(
        models.NotificationPreference.user_id == user_id,
        models.NotificationPreference.deleted_at.is_(None),
    )


def get_preferences_map(db: Session, user_id: int) -> Dict[str, Dict[str, bool]]:
    """Every category x channel for this user, defaulting missing rows to
    ``True`` (enabled)."""
    result = {cat: {ch: True for ch in CHANNELS} for cat in CATEGORIES}
    rows = _query(db, user_id).all()
    for row in rows:
        if row.category in result and row.channel in CHANNELS:
            result[row.category][row.channel] = bool(row.enabled)
    return result


def get_row(db: Session, user_id: int, category: str, channel: str) -> Optional["models.NotificationPreference"]:
    return _query(db, user_id).filter(
        models.NotificationPreference.category == category,
        models.NotificationPreference.channel == channel,
    ).first()


def is_enabled(db: Session, *, user_id: int, notification_type: Optional[str], channel: str) -> bool:
    """True unless the user has an explicit, LIVE disabling row for this
    type's category + channel. A type with no category (no UI toggle) is
    always enabled."""
    category = category_for_type(notification_type)
    if category is None:
        return True
    row = get_row(db, user_id, category, channel)
    if row is None:
        return True
    return bool(row.enabled)


def set_preferences(
    db: Session, user_id: int, updates: Dict[str, Dict[str, Optional[bool]]]
) -> Dict[str, Dict[str, bool]]:
    """Upsert the given (category -> {channel: enabled}) updates for this
    user. Unknown categories/channels and ``None`` values are ignored
    (partial update). Commits. Returns the full resulting preference map."""
    for category, channels in (updates or {}).items():
        if category not in CATEGORIES or not channels:
            continue
        for channel, enabled in channels.items():
            if channel not in CHANNELS or enabled is None:
                continue
            row = get_row(db, user_id, category, channel)
            if row is None:
                row = models.NotificationPreference(
                    user_id=user_id, category=category, channel=channel,
                    enabled=bool(enabled),
                )
                db.add(row)
            else:
                row.enabled = bool(enabled)
    db.commit()
    return get_preferences_map(db, user_id)
