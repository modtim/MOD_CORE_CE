"""Changelog / Release Notes Router (a.k.a. "What's New", #5).

Admin endpoints for authoring + scheduling + publishing release
notes. Public endpoints for users to read what shipped recently.
Unauthenticated public URL at `/whats-new/<slug>` serves a
server-rendered HTML stub with Open Graph meta so the link
previews well on social and the dashboard SPA picks it up for
authenticated visits.

Capability: `whats-new-publishing` under openspec change
`dashboard-prelaunch-overhaul`.
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional, List, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

import models
from dependencies import get_db, require_active_and_onboarded_user
from database import SessionLocal

from services import changelog_service
from services.changelog_service import _SLUG_RE
from services._base import NotFoundError

router = APIRouter(tags=["changelog"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------

class ChangelogCreate(BaseModel):
    title: str
    body: str
    version: Optional[str] = None
    is_published: bool = False
    summary: Optional[str] = None
    hero_image_url: Optional[str] = None
    tags: Optional[List[str]] = None
    slug: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    # 'user_facing' or 'internal'. Defaults to 'internal' on the
    # model — admins SHALL pick explicitly when publishing.
    audience: Optional[str] = None


class ChangelogUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    version: Optional[str] = None
    is_published: Optional[bool] = None
    summary: Optional[str] = None
    hero_image_url: Optional[str] = None
    tags: Optional[List[str]] = None
    slug: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    audience: Optional[str] = None


class ChangelogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    body: str
    version: Optional[str]
    is_published: bool
    published_at: Optional[str]
    created_at: str
    summary: Optional[str] = None
    hero_image_url: Optional[str] = None
    tags: Optional[List[str]] = None
    slug: Optional[str] = None
    scheduled_for: Optional[str] = None
    audience: Optional[str] = None


def _to_resp(e: models.ChangelogEntry) -> ChangelogResponse:
    return ChangelogResponse(
        id=e.id,
        title=e.title,
        body=e.body,
        version=e.version,
        is_published=e.is_published,
        published_at=e.published_at.isoformat() if e.published_at else None,
        created_at=e.created_at.isoformat() if e.created_at else "",
        summary=e.summary,
        hero_image_url=e.hero_image_url,
        tags=e.tags or [],
        slug=e.slug,
        scheduled_for=e.scheduled_for.isoformat() if e.scheduled_for else None,
        audience=getattr(e, "audience", None),
    )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _require_platform_admin(current_user: models.User = Depends(require_active_and_onboarded_user)):
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform admin required")
    return current_user


def _escape_html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# ---------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------

@router.get("/changelog", response_model=List[ChangelogResponse])
def list_changelog(
    db: Session = Depends(get_db),
    user: models.User = Depends(require_active_and_onboarded_user),
    limit: int = 20,
    offset: int = 0,
    tag: Optional[str] = None,
):
    """List published changelog entries (newest first). Optional
    `tag` filter is matched against `tags` JSONB array.

    Platform admins ALWAYS see the full feed (user_facing + internal
    entries) — no opt-in flag required. Tenant users only ever see
    `audience='user_facing'`. The audience filter is server-enforced
    so a malicious / misconfigured client can never see internal
    entries it shouldn't."""
    entries = changelog_service.list_visible(
        db,
        is_platform_admin=user.is_platform_admin,
        limit=limit,
        offset=offset,
        tag=tag,
    )
    return [_to_resp(e) for e in entries]


@router.get("/changelog/latest", response_model=Optional[ChangelogResponse])
def get_latest_changelog(
    db: Session = Depends(get_db),
    user: models.User = Depends(require_active_and_onboarded_user),
):
    """Get the most recent published entry visible to the caller.
    Platform admins see the latest of any audience; tenant users see
    only the latest user_facing entry. Backs the dashboard shell's
    'What's New' bell-dot badge."""
    entry = changelog_service.get_latest_visible(db, is_platform_admin=user.is_platform_admin)
    return _to_resp(entry) if entry else None


@router.get("/changelog/by-slug/{slug}", response_model=ChangelogResponse)
def get_changelog_by_slug(
    slug: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_active_and_onboarded_user),
):
    """Per-note read by slug — backs the dashboard's
    `#changelog?slug=...` deep-link route. Platform admins can fetch
    any audience by slug; for tenant users, internal entries 404
    (so a leaked slug URL doesn't disclose the entry)."""
    try:
        entry = changelog_service.get_by_slug_visible(db, slug, is_platform_admin=user.is_platform_admin)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)
    return _to_resp(entry)


# ---------------------------------------------------------------------
# Admin authoring — gated by Depends(_require_platform_admin), not the URL
# (clean-v2-api admin-dissolve wave: moved off `/admin/changelog/*` onto the
# same `/changelog` resource as the public reads; only changelog.js calls
# these, already repointed, so no back-compat alias needed).
# ---------------------------------------------------------------------

@router.get("/changelog/drafts", response_model=List[ChangelogResponse])
def list_drafts(
    db: Session = Depends(get_db),
    _admin: models.User = Depends(_require_platform_admin),
):
    """List unpublished entries (drafts + scheduled-future). Admin only."""
    entries = changelog_service.list_drafts(db)
    return [_to_resp(e) for e in entries]


@router.post("/changelog", response_model=ChangelogResponse, status_code=status.HTTP_201_CREATED)
def create_changelog(
    data: ChangelogCreate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(_require_platform_admin),
):
    """Create a new changelog entry. Set `is_published=true` for an
    immediate publish; set `scheduled_for=<future-iso>` for a
    scheduled publish (the background `publish_scheduled_changelogs`
    helper flips it on time)."""
    try:
        entry = changelog_service.create_entry(
            db,
            title=data.title,
            body=data.body,
            version=data.version,
            summary=data.summary,
            hero_image_url=data.hero_image_url,
            tags=data.tags,
            slug=data.slug,
            scheduled_for=data.scheduled_for,
            is_published=data.is_published,
            audience=data.audience,
            admin_id=admin.id,
        )
    except changelog_service.ChangelogValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    return _to_resp(entry)


@router.put("/changelog/{entry_id}", response_model=ChangelogResponse)
def update_changelog(
    entry_id: int,
    data: ChangelogUpdate,
    db: Session = Depends(get_db),
    _admin: models.User = Depends(_require_platform_admin),
):
    try:
        entry = changelog_service.update_entry(
            db,
            entry_id,
            title=data.title,
            body=data.body,
            version=data.version,
            summary=data.summary,
            hero_image_url=data.hero_image_url,
            tags=data.tags,
            slug=data.slug,
            scheduled_for=data.scheduled_for,
            audience=data.audience,
            is_published=data.is_published,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)
    except changelog_service.ChangelogValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    return _to_resp(entry)


@router.delete("/changelog/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_changelog(
    entry_id: int,
    db: Session = Depends(get_db),
    _admin: models.User = Depends(_require_platform_admin),
):
    try:
        changelog_service.delete_entry(db, entry_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message)


# ---------------------------------------------------------------------
# Public unauthenticated URL with Open Graph metadata
# ---------------------------------------------------------------------

_DASHBOARD_URL_DEFAULT = "https://example.com"
_OG_DEFAULT_IMAGE = "https://example.com/og-default.png"


def _dashboard_base_url() -> str:
    return os.getenv("DASHBOARD_PUBLIC_URL") \
        or os.getenv("PUBLIC_DASHBOARD_URL") \
        or _DASHBOARD_URL_DEFAULT


def _og_html(entry: models.ChangelogEntry) -> str:
    dashboard_url = _dashboard_base_url().rstrip("/")
    deep_link = f"{dashboard_url}/#changelog?slug={entry.slug}"
    title = entry.title or "What's New — MOD Platform"
    description = entry.summary or (entry.body or "")[:200].replace("\n", " ")
    image = entry.hero_image_url or _OG_DEFAULT_IMAGE
    canonical = f"{dashboard_url}/whats-new/{entry.slug}"
    # Body is escaped + Markdown is rendered server-side as a
    # minimal subset so social-media bots see real content (not
    # an empty page).
    body_html = _render_markdown(entry.body or "")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{_escape_html(title)} — MOD Platform</title>
  <meta name="description" content="{_escape_html(description)}">
  <link rel="canonical" href="{_escape_html(canonical)}">

  <!-- Open Graph -->
  <meta property="og:title" content="{_escape_html(title)}">
  <meta property="og:description" content="{_escape_html(description)}">
  <meta property="og:image" content="{_escape_html(image)}">
  <meta property="og:url" content="{_escape_html(canonical)}">
  <meta property="og:type" content="article">
  <meta property="og:site_name" content="MOD Tech Labs">
  {f'<meta property="article:published_time" content="{entry.published_at.isoformat()}">'
     if entry.published_at else ''}

  <!-- Twitter -->
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{_escape_html(title)}">
  <meta name="twitter:description" content="{_escape_html(description)}">
  <meta name="twitter:image" content="{_escape_html(image)}">

  <style>
    :root {{ --brand: #0891B2; --bg: #F8FAFC; --text: #0F172A; --muted: #475569; }}
    body {{ font-family: 'Inter', system-ui, -apple-system, sans-serif;
            background: var(--bg); color: var(--text); margin: 0; padding: 40px 20px;
            line-height: 1.6; }}
    main {{ max-width: 720px; margin: 0 auto; background: white;
            border-radius: 12px; padding: 32px;
            box-shadow: 0 4px 12px rgba(15, 23, 42, 0.08); }}
    h1 {{ font-size: 28px; margin: 0 0 8px; }}
    .meta {{ color: var(--muted); font-size: 14px; margin-bottom: 16px; }}
    .summary {{ color: var(--muted); font-size: 16px; margin-bottom: 24px; }}
    .hero {{ width: 100%; max-height: 360px; object-fit: cover;
             border-radius: 8px; margin-bottom: 24px; }}
    .body {{ font-size: 16px; }}
    .body strong {{ color: var(--text); }}
    .body code {{ background: var(--bg); padding: 1px 6px; border-radius: 4px;
                 font-size: 14px; }}
    .body h4 {{ font-size: 16px; margin: 24px 0 8px; color: var(--text); }}
    .body li {{ margin-left: 20px; }}
    .cta {{ display: inline-block; margin-top: 24px; background: var(--brand);
            color: white; text-decoration: none; padding: 10px 20px;
            border-radius: 8px; font-weight: 600; }}
    .footer {{ margin-top: 32px; color: var(--muted); font-size: 13px;
               text-align: center; }}
  </style>
</head>
<body>
  <main>
    <h1>{_escape_html(title)}</h1>
    <div class="meta">
      {f'<span style="color:var(--brand);font-weight:600;">{_escape_html(entry.version)}</span> · '
         if entry.version else ''}
      {entry.published_at.strftime('%B %d, %Y') if entry.published_at else ''}
    </div>
    {f'<p class="summary">{_escape_html(entry.summary)}</p>' if entry.summary else ''}
    {f'<img src="{_escape_html(entry.hero_image_url)}" alt="" class="hero">' if entry.hero_image_url else ''}
    <div class="body">{body_html}</div>
    <a class="cta" href="{_escape_html(deep_link)}">Open in MOD Dashboard →</a>
    <p class="footer">
      Posted to <a href="{_escape_html(dashboard_url)}" style="color:var(--brand)">MOD Tech Labs</a>'s
      What's New on {entry.published_at.strftime('%B %d, %Y') if entry.published_at else 'recently'}.
    </p>
  </main>
</body>
</html>"""


def _render_markdown(body: str) -> str:
    """Minimal server-side Markdown subset matching the dashboard
    renderer in `js/changelog.js` (bold, inline code, bullet lists,
    `### h4` headings). Output is HTML-safe.
    """
    if not body:
        return ""
    out = _escape_html(body)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    lines = []
    in_list = False
    for line in out.split("\n"):
        if line.startswith("### "):
            if in_list:
                lines.append("</ul>"); in_list = False
            lines.append(f"<h4>{line[4:]}</h4>")
        elif line.startswith("- "):
            if not in_list:
                lines.append("<ul>"); in_list = True
            lines.append(f"<li>{line[2:]}</li>")
        else:
            if in_list:
                lines.append("</ul>"); in_list = False
            if line.strip():
                lines.append(f"<p>{line}</p>")
    if in_list:
        lines.append("</ul>")
    return "\n".join(lines)


# api-lint: public-by-design: deliberately public release-note share link for social-media link previews, see handler docstring
@router.get("/whats-new/{slug}", response_class=HTMLResponse, include_in_schema=False)
def public_whats_new_html(slug: str):
    """Unauthenticated public URL for sharing a release note (#5 /
    task 17.6). Returns server-rendered HTML with Open Graph meta
    tags so the link previews well on social media. Drafts and
    scheduled-future entries 404; only published entries are
    shareable.

    No auth dependency — this is the only endpoint in the API
    that's deliberately public. The full body is rendered
    server-side so social-media crawlers see real content.
    """
    db = SessionLocal()
    try:
        try:
            entry = changelog_service.get_public_by_slug(db, slug)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=exc.message)
        return HTMLResponse(content=_og_html(entry), status_code=200)
    finally:
        db.close()


# ---------------------------------------------------------------------
# Scheduled-publish worker hook
# ---------------------------------------------------------------------

def publish_scheduled_changelogs(db: Session) -> int:
    """Flip drafts to live once `scheduled_for` <= now. Designed to
    be called from the periodic system-tasks loop. Returns the
    number of entries published this tick."""
    return changelog_service.publish_scheduled(db)
