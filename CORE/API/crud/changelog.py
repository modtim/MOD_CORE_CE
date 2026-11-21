"""Changelog / Release Notes CRUD (query layer only).

The ONLY place `db.query(models.ChangelogEntry)` lives for this domain.
Business policy (audience visibility, slug-collision resolution, publish
gating, notification fan-out) lives in `services/changelog_service.py`.

Note: `ChangelogEntry` has neither `tenant_id` nor `deleted_at` — it is a
platform-wide feed (not per-tenant data) and reads are visibility-scoped
by `audience`, not tenant. There is also no soft-delete column on this
model today, so `delete_entry` below is a HARD delete (see ISO flag in
the migration report — pre-existing, out of scope for this migration).
"""
from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

import models


def get_by_id(db: Session, entry_id: int) -> Optional[models.ChangelogEntry]:
    """Fetch one entry by primary key, UN-scoped by audience. Callers
    apply visibility/authorization policy themselves (keeps a legit
    403 from a caller-side check from collapsing into a 404 here)."""
    return db.query(models.ChangelogEntry).filter(
        models.ChangelogEntry.id == entry_id,
    ).first()


def get_by_slug(
    db: Session,
    slug: str,
    *,
    published_only: bool = True,
) -> Optional[models.ChangelogEntry]:
    """Fetch one entry by slug, with no audience restriction (used by
    the deliberately-public `/whats-new/{slug}` route)."""
    query = db.query(models.ChangelogEntry).filter(models.ChangelogEntry.slug == slug)
    if published_only:
        query = query.filter(models.ChangelogEntry.is_published == True)  # noqa: E712
    return query.first()


def slug_exists(db: Session, slug: str, *, exclude_id: Optional[int] = None) -> bool:
    # include_deleted: `slug` is UNIQUE at the DB level (not partial), so a
    # tombstoned entry still owns its slug. Uniqueness/collision-resolution
    # must see tombstones or a re-created entry would collide on INSERT.
    query = (
        db.query(models.ChangelogEntry)
        .execution_options(include_deleted=True)
        .filter(models.ChangelogEntry.slug == slug)
    )
    if exclude_id is not None:
        query = query.filter(models.ChangelogEntry.id != exclude_id)
    return query.first() is not None


def list_published(
    db: Session,
    *,
    audience: Optional[str],
    tag: Optional[str],
    limit: int,
    offset: int,
) -> List[models.ChangelogEntry]:
    """Published entries, newest first. `audience=None` means "no
    audience restriction" (platform admin); any other value restricts
    the feed to that audience (tenant user)."""
    query = db.query(models.ChangelogEntry).filter(
        models.ChangelogEntry.is_published == True,  # noqa: E712
    )
    if audience is not None:
        query = query.filter(models.ChangelogEntry.audience == audience)
    if tag:
        # JSONB array contains check. Use the explicit `@>` Postgres
        # operator — `Column.contains` on the project's JSONB
        # TypeDecorator falls through to the LIKE codepath.
        query = query.filter(
            models.ChangelogEntry.tags.op("@>")(_json.dumps([tag]))
        )
    return query.order_by(
        models.ChangelogEntry.published_at.desc(),
    ).offset(offset).limit(limit).all()


def get_latest_published(db: Session, *, audience: Optional[str]) -> Optional[models.ChangelogEntry]:
    query = db.query(models.ChangelogEntry).filter(
        models.ChangelogEntry.is_published == True,  # noqa: E712
    )
    if audience is not None:
        query = query.filter(models.ChangelogEntry.audience == audience)
    return query.order_by(models.ChangelogEntry.published_at.desc()).first()


def get_published_by_slug(db: Session, slug: str, *, audience: Optional[str]) -> Optional[models.ChangelogEntry]:
    query = db.query(models.ChangelogEntry).filter(
        models.ChangelogEntry.slug == slug,
        models.ChangelogEntry.is_published == True,  # noqa: E712
    )
    if audience is not None:
        query = query.filter(models.ChangelogEntry.audience == audience)
    return query.first()


def list_drafts(db: Session) -> List[models.ChangelogEntry]:
    """Unpublished entries (drafts + scheduled-future), newest first."""
    return db.query(models.ChangelogEntry).filter(
        models.ChangelogEntry.is_published == False,  # noqa: E712
    ).order_by(models.ChangelogEntry.created_at.desc()).all()


def list_due_scheduled(db: Session, *, now: datetime) -> List[models.ChangelogEntry]:
    """Drafts whose `scheduled_for` has passed. Read-only — the
    service layer owns flipping them live + the commit boundary."""
    return db.query(models.ChangelogEntry).filter(
        models.ChangelogEntry.is_published == False,  # noqa: E712
        models.ChangelogEntry.scheduled_for.isnot(None),
        models.ChangelogEntry.scheduled_for <= now,
    ).all()


def list_active_users(db: Session) -> List[models.User]:
    """Active users to fan a release-published notification out to."""
    return db.query(models.User).filter(
        models.User.status == models.UserStatus.ACTIVE,
    ).all()


def create(db: Session, values: dict) -> models.ChangelogEntry:
    entry = models.ChangelogEntry(**values)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def add_notification(db: Session, *, values: dict) -> None:
    """Queue one notification row (caller commits once after the fan-out
    loop, matching the pre-migration batching behaviour)."""
    db.add(models.Notification(**values))


def hard_delete(db: Session, entry: models.ChangelogEntry) -> None:
    """Soft-delete a changelog entry.

    iso-soft-delete-sweep (2026-09-22): ISO 27001 no-hard-delete (MEMORY
    iso-no-hard-delete). `ChangelogEntry` now has a `deleted_at` column
    (SoftDeleteMixin) and is in database.py's soft-delete listener, so the
    tombstoned entry drops out of every live feed. Name kept for its callers.
    """
    entry.deleted_at = datetime.now(timezone.utc)
    db.commit()
