"""Single source of truth for the platform GitHub token.

The node-wizard describe cascade (node_builder), the repo-fetch endpoints, and
github_search all resolve the platform GitHub token through here, so ONE
configured value covers every GitHub call (no more divergent
`GITHUB_PLATFORM_TOKEN` env vs `github_token` secret).

Resolution order:
  1. The platform-tenant ``github_token`` secret (Vault-backed; configurable in
     the Dashboard → Settings → Secrets) — the canonical source.
  2. Env fallback: ``GITHUB_PLATFORM_TOKEN`` then ``GITHUB_TOKEN`` (ops/dev).

Returns None when unset; the ``PLEASE_CONFIGURE`` placeholder counts as unset.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_PLATFORM_TENANT_ID = 1
_PLACEHOLDER = "PLEASE_CONFIGURE"
_ENV_NAMES = ("GITHUB_PLATFORM_TOKEN", "GITHUB_TOKEN")


def get_platform_github_token(db=None) -> Optional[str]:
    """Resolve the platform GitHub token: canonical ``github_token`` secret
    first (when a DB session is available), then env fallback. None if unset."""
    if db is not None:
        try:
            import crud
            val = crud.get_decrypted_secret_by_name(db, "github_token", _PLATFORM_TENANT_ID)
            if val and val.strip() and val.strip() != _PLACEHOLDER:
                return val.strip()
        except Exception as e:  # noqa: BLE001 — a Vault hiccup must not break callers
            logger.debug("github token secret lookup failed: %s", e)
    for name in _ENV_NAMES:
        v = (os.getenv(name) or "").strip()
        if v:
            return v
    return None
