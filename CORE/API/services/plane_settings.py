"""services/plane_settings.py — Plane board coordinates as platform settings
(Round 42, OWNER-DECISIONS.md:867 + lead_queue.md:692, owner 10-12).

The Plane connector nodes (seed_plane_node.py, launch-completion-megaplan
20.3.s3) used to seed `plane_base_url` / `workspace_slug` / `project_id` with
empty defaults because the owner hadn't supplied values. The owner has now
supplied them, and per CORE instructions tunables are DB-backed platform
settings registered through `platform_settings.register(...)` — never env
vars, never new columns. This module is the registration + reader half;
seed_plane_node.py hydrates its seeded param defaults from these readers.

Keys (all `group="talk"` — Group L / talk-to-task):
- `talk.plane_base_url` (str) — base URL of the Plane board.
- `talk.plane_workspace_slug` (str) — workspace slug (URL path segment).
- `talk.plane_project_id` (str) — project (board) id the nodes act on.

Resolution is the registry's own precedence: tenant row > platform row >
env fallback > registered default. A read failure falls back to the
registered default, never raises: the seed must still produce a usable
schema when the settings layer is unhappy.

PLANE_API_KEY is NOT a setting and appears nowhere here — it stays a
secret_ref (custom_metadata.accepted_secrets) resolved at runtime by the
worker, a separate mechanism from platform settings.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from services import platform_settings as psr  # leaf module: no audit cycle

logger = logging.getLogger(__name__)

GROUP = "talk"

BASE_URL_KEY = "talk.plane_base_url"
WORKSPACE_SLUG_KEY = "talk.plane_workspace_slug"
PROJECT_ID_KEY = "talk.plane_project_id"

# Previously example.com (MOD's own board); CE ships a
# neutral placeholder so every new install must configure talk.plane_base_url.
DEFAULT_BASE_URL = "https://plane.example.com"
DEFAULT_WORKSPACE_SLUG = "mod-tech-labs"
DEFAULT_PROJECT_ID = "20d97fd0-35bd-4db7-89a5-4b805bb1d7a6"


def plane_base_url(db: Session = None,
                   tenant_id: Optional[int] = None) -> str:
    """Base URL of the Plane board; platform-row default unless a tenant
    overrides. A read failure yields the registered default, never raises.
    """
    try:
        value = psr.get_setting(BASE_URL_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — fall back to the registered default
        logger.warning("plane_settings: base-url-read failed; using the "
                       "registered default")
        return DEFAULT_BASE_URL
    return str(value or DEFAULT_BASE_URL).strip()


def plane_workspace_slug(db: Session = None,
                         tenant_id: Optional[int] = None) -> str:
    """Plane workspace slug; same precedence + fallback as the base URL."""
    try:
        value = psr.get_setting(WORKSPACE_SLUG_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — fall back to the registered default
        logger.warning("plane_settings: slug-read failed; using the "
                       "registered default")
        return DEFAULT_WORKSPACE_SLUG
    return str(value or DEFAULT_WORKSPACE_SLUG).strip()


def plane_project_id(db: Session = None,
                     tenant_id: Optional[int] = None) -> str:
    """Plane project (board) id; same precedence + fallback as the base URL."""
    try:
        value = psr.get_setting(PROJECT_ID_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — fall back to the registered default
        logger.warning("plane_settings: project-id-read failed; using the "
                       "registered default")
        return DEFAULT_PROJECT_ID
    return str(value or DEFAULT_PROJECT_ID).strip()


def _register_settings() -> None:
    psr.register(
        BASE_URL_KEY,
        str,
        DEFAULT_BASE_URL,
        group=GROUP,
        description="Round 42 — base URL of the Plane board the talk-to-task "
                    "Plane nodes act on (owner-supplied; no trailing slash). "
                    "A tenant row can point its nodes at a different board.",
    )
    psr.register(
        WORKSPACE_SLUG_KEY,
        str,
        DEFAULT_WORKSPACE_SLUG,
        group=GROUP,
        description="Round 42 — Plane workspace slug (the URL path segment "
                    "after the base URL) for the talk-to-task Plane nodes.",
    )
    psr.register(
        PROJECT_ID_KEY,
        str,
        DEFAULT_PROJECT_ID,
        group=GROUP,
        description="Round 42 — Plane project (board) id the list/upsert "
                    "nodes read and write; owner-supplied for this tenant.",
    )


_register_settings()
