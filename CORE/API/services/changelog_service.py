"""Changelog / Release Notes business logic (Gate 2b migration).

`routers/changelog.py` stays HTTP-only: parse input, resolve the
caller's admin/visibility inputs from auth, call here, map service
errors to `HTTPException`. This module owns the transaction boundary
and all policy — audience gating, slug-collision resolution, publish
gating, and the release-published notification fan-out. All
`db.query(models.ChangelogEntry)` for this domain lives in
`crud/changelog.py`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

import models
import crud.changelog as changelog_crud
from services._base import NotFoundError, ServiceError

logger = logging.getLogger(__name__)

# Re-exported by routers/changelog.py so `changelog._SLUG_RE` (used
# directly by tests) keeps working unchanged.
_SLUG_RE = re.compile(r"[^a-z0-9]+")

_ALLOWED_AUDIENCES = ("user_facing", "internal")
_DEFAULT_AUDIENCE = "internal"


class ChangelogValidationError(ServiceError):
    """A policy violation the router maps to 400 (e.g. missing
    audience on publish)."""

    def __init__(self, message: str):
        super().__init__(message, code="bad_request")


def _audience_filter(is_platform_admin: bool) -> Optional[str]:
    """Platform admins see every audience; everyone else is restricted
    to 'user_facing'. `None` means "no restriction"."""
    return None if is_platform_admin else "user_facing"


def generate_slug(db: Session, title: str, *, exclude_id: Optional[int] = None) -> str:
    """URL-safe slug from a title; appends -2, -3, … on collision."""
    base = _SLUG_RE.sub("-", (title or "release").lower()).strip("-") or "release"
    base = base[:140]
    candidate = base
    n = 2
    while changelog_crud.slug_exists(db, candidate, exclude_id=exclude_id):
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _emit_published_notification(db: Session, entry: models.ChangelogEntry) -> None:
    """Best-effort in-app notification when a release transitions to
    published. Notifies every active user; the bell-icon dot in the
    Dashboard surfaces it. Failure is non-fatal — the publish itself
    has already committed.
    """
    try:
        from routers.notifications import publish_notification_event
    except Exception:
        publish_notification_event = None
    try:
        users = changelog_crud.list_active_users(db)
        notif_text = entry.summary or (entry.body or "")[:200]
        link_url = f"#changelog?slug={entry.slug}" if entry.slug else "#changelog"
        from crud import notification_preferences as notification_prefs_crud
        for u in users:
            # WS4.Q22: honour each user's product in_app preference
            # (missing row == enabled).
            if not notification_prefs_crud.is_enabled(
                db, user_id=u.id, notification_type="release_published",
                channel="in_app",
            ):
                continue
            # models.Notification has no tenant_id column (it is reached
            # tenant-scoped only through recipient_user_id) — passing it
            # raised TypeError on every call, silently swallowed by the
            # broad except below, so no release notification was ever
            # actually inserted (same bug class as stripe_webhooks'
            # _notify_tenant_admins, fixed in GB.PAY.11).
            changelog_crud.add_notification(db, values=dict(
                recipient_user_id=u.id,
                notification_type="release_published",
                message=f"What's new: {entry.title} — {notif_text}",
                link_url=link_url,
                is_read=False,
                is_archived=False,
            ))
        db.commit()
        if publish_notification_event:
            for u in users:
                try:
                    publish_notification_event(u.id)
                except Exception:
                    pass
    except Exception:
        logger.exception("release-published notification fan-out failed")


# ---------------------------------------------------------------------
# Public reads
# ---------------------------------------------------------------------

def list_visible(
    db: Session,
    *,
    is_platform_admin: bool,
    limit: int,
    offset: int,
    tag: Optional[str],
) -> List[models.ChangelogEntry]:
    """List published changelog entries (newest first). Optional `tag`
    filter is matched against `tags` JSONB array.

    Platform admins ALWAYS see the full feed (user_facing + internal
    entries) — no opt-in flag required. Tenant users only ever see
    `audience='user_facing'`. The audience filter is server-enforced
    so a malicious / misconfigured client can never see internal
    entries it shouldn't."""
    return changelog_crud.list_published(
        db,
        audience=_audience_filter(is_platform_admin),
        tag=tag,
        limit=limit,
        offset=offset,
    )


def get_latest_visible(db: Session, *, is_platform_admin: bool) -> Optional[models.ChangelogEntry]:
    """Most recent published entry visible to the caller. Platform
    admins see the latest of any audience; tenant users see only the
    latest user_facing entry."""
    return changelog_crud.get_latest_published(db, audience=_audience_filter(is_platform_admin))


def get_by_slug_visible(db: Session, slug: str, *, is_platform_admin: bool) -> models.ChangelogEntry:
    """Per-note read by slug. Platform admins can fetch any audience by
    slug; for tenant users, internal entries 404 (so a leaked slug URL
    doesn't disclose the entry)."""
    entry = changelog_crud.get_published_by_slug(db, slug, audience=_audience_filter(is_platform_admin))
    if not entry:
        raise NotFoundError("Release note not found")
    return entry


def get_public_by_slug(db: Session, slug: str) -> models.ChangelogEntry:
    """Unauthenticated public read — only published entries, no
    audience restriction (there's no caller identity to restrict by)."""
    entry = changelog_crud.get_by_slug(db, slug, published_only=True)
    if not entry:
        raise NotFoundError("Release note not found")
    return entry


def list_drafts(db: Session) -> List[models.ChangelogEntry]:
    """List unpublished entries (drafts + scheduled-future). Admin only
    (enforced by the router's auth dependency)."""
    return changelog_crud.list_drafts(db)


# ---------------------------------------------------------------------
# Admin authoring
# ---------------------------------------------------------------------

def create_entry(
    db: Session,
    *,
    title: str,
    body: str,
    version: Optional[str],
    summary: Optional[str],
    hero_image_url: Optional[str],
    tags: Optional[List[str]],
    slug: Optional[str],
    scheduled_for: Optional[datetime],
    is_published: bool,
    audience: Optional[str],
    admin_id: int,
) -> models.ChangelogEntry:
    """Create a new changelog entry. `is_published=True` for an
    immediate publish; `scheduled_for=<future>` for a scheduled publish
    (the background `publish_scheduled` helper flips it on time)."""
    resolved_slug = slug or generate_slug(db, title)
    publish_now = bool(is_published) and not scheduled_for
    # Audience: default to 'internal' on draft (safe default — a
    # forgotten flag never leaks to tenants). On publish (immediate or
    # scheduled) the admin SHALL pick explicitly — reject if missing.
    publish_intent = publish_now or scheduled_for is not None
    if publish_intent and audience not in _ALLOWED_AUDIENCES:
        raise ChangelogValidationError(
            "audience required on publish: pick 'user_facing' or 'internal'"
        )
    resolved_audience = audience or _DEFAULT_AUDIENCE
    entry = changelog_crud.create(db, dict(
        title=title,
        body=body,
        version=version,
        summary=summary,
        hero_image_url=hero_image_url,
        tags=tags or [],
        slug=resolved_slug,
        scheduled_for=scheduled_for,
        is_published=publish_now,
        published_at=datetime.now(timezone.utc) if publish_now else None,
        created_by_user_id=admin_id,
        audience=resolved_audience,
    ))
    if publish_now:
        _emit_published_notification(db, entry)
    return entry


def update_entry(
    db: Session,
    entry_id: int,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    version: Optional[str] = None,
    summary: Optional[str] = None,
    hero_image_url: Optional[str] = None,
    tags: Optional[List[str]] = None,
    slug: Optional[str] = None,
    scheduled_for: Optional[datetime] = None,
    audience: Optional[str] = None,
    is_published: Optional[bool] = None,
) -> models.ChangelogEntry:
    entry = changelog_crud.get_by_id(db, entry_id)
    if not entry:
        raise NotFoundError("Changelog entry not found")

    was_published = entry.is_published
    if title is not None:
        entry.title = title
    if body is not None:
        entry.body = body
    if version is not None:
        entry.version = version
    if summary is not None:
        entry.summary = summary
    if hero_image_url is not None:
        entry.hero_image_url = hero_image_url
    if tags is not None:
        entry.tags = tags
    if slug is not None:
        entry.slug = slug or generate_slug(db, entry.title, exclude_id=entry.id)
    if scheduled_for is not None:
        entry.scheduled_for = scheduled_for
    if audience is not None:
        if audience not in _ALLOWED_AUDIENCES:
            raise ChangelogValidationError("audience must be 'user_facing' or 'internal'")
        entry.audience = audience
    # Block the publish flip if the entry has no explicit audience —
    # forces the admin to pick before tenants could see the entry.
    if is_published is not None:
        if is_published and entry.audience not in _ALLOWED_AUDIENCES:
            raise ChangelogValidationError(
                "audience required to publish: pick 'user_facing' or 'internal'"
            )
        entry.is_published = is_published
        if is_published and not entry.published_at:
            entry.published_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(entry)
    if entry.is_published and not was_published:
        _emit_published_notification(db, entry)
    return entry


def delete_entry(db: Session, entry_id: int) -> None:
    """Soft-delete a changelog entry.

    ISO 27001 (iso-no-hard-delete): `ChangelogEntry` now carries a
    `deleted_at` column (SoftDeleteMixin) and is registered in
    database.py's soft-delete listener, so `changelog_crud.hard_delete`
    tombstones the row (sets `deleted_at`) rather than removing it — the
    entry drops out of every live feed while the record is retained. The
    CRUD name is kept for its callers.
    """
    entry = changelog_crud.get_by_id(db, entry_id)
    if not entry:
        raise NotFoundError("Changelog entry not found")
    changelog_crud.hard_delete(db, entry)


# ---------------------------------------------------------------------
# Scheduled-publish worker hook
# ---------------------------------------------------------------------

def publish_scheduled(db: Session) -> int:
    """Flip drafts to live once `scheduled_for` <= now. Designed to be
    called from the periodic system-tasks loop. Returns the number of
    entries published this tick."""
    now = datetime.now(timezone.utc)
    rows = changelog_crud.list_due_scheduled(db, now=now)
    if not rows:
        return 0
    count = 0
    for entry in rows:
        entry.is_published = True
        entry.published_at = entry.scheduled_for or now
        db.add(entry)
        count += 1
    db.commit()
    for entry in rows:
        _emit_published_notification(db, entry)
    logger.info("publish_scheduled_changelogs: %d entries published", count)
    return count
