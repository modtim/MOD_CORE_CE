"""
Worker release manifest helpers.

The API used to consult a `WORKER_AGENT_VERSION` env var to decide whether
an update was available for a worker. That was fragile — operators had
to remember to bump the env after every build, and the version string
flowing out to the Dashboard was a raw git-describe (e.g.
`fivekproject-733-ge109fd2e`) rather than a human-readable semver.

Now the source of truth is a JSON manifest at
    worker-releases/latest.json

uploaded alongside the platform binaries by `CORE/Worker/scripts/release.sh`.
The manifest shape:

    {
      "version":       "v1.0.1",
      "released_at":   "2026-04-15T16:30:00Z",
      "changelog_url": "https://...",                (optional)
      "amd64_sha256":  "hex digest of linux-amd64 binary",
      "arm64_sha256":  "hex digest of linux-arm64 binary",
      "notes":         "free-form operator notes"     (optional)
    }

All fields are optional except `version`. Missing manifests fall back to
the legacy env-var behaviour so existing deployments keep working during
the migration window. Cached for 60 seconds — operators shouldn't need
to wait more than a minute after uploading a new manifest for the
Dashboard to reflect it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MANIFEST_OBJECT_NAME = "latest.json"
WORKER_RELEASES_BUCKET = "worker-releases"

_CACHE: Dict[str, Any] = {"manifest": None, "fetched_at": 0.0, "error": None}
_CACHE_TTL_SECONDS = 60.0


def _fetch_manifest_from_minio() -> Optional[Dict[str, Any]]:
    """Read and parse worker-releases/latest.json from MinIO. None on any error."""
    try:
        from utils.storage import get_service_client
        client = get_service_client()
        response = client.get_object(WORKER_RELEASES_BUCKET, MANIFEST_OBJECT_NAME)
        try:
            data = json.loads(response.read().decode("utf-8"))
        finally:
            response.close()
            response.release_conn()
        if not isinstance(data, dict) or not data.get("version"):
            logger.warning("worker-releases manifest missing 'version' field: %r", data)
            return None
        return data
    except Exception as e:
        # "Object not found" is the common case when no manifest has been
        # uploaded yet — we log at debug so it's not noisy.
        msg = str(e)
        if "NoSuchKey" in msg or "does not exist" in msg.lower():
            logger.debug("No worker-releases manifest present yet: %s", msg)
        else:
            logger.warning("Failed to fetch worker-releases manifest: %s", e)
        return None


def get_manifest(force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    """
    Return the cached manifest dict, refreshing from MinIO if the cache has
    expired. Returns None if no manifest exists or MinIO is unreachable.
    """
    now = time.time()
    if (
        not force_refresh
        and _CACHE["manifest"] is not None
        and (now - _CACHE["fetched_at"]) < _CACHE_TTL_SECONDS
    ):
        return _CACHE["manifest"]
    manifest = _fetch_manifest_from_minio()
    _CACHE["manifest"] = manifest
    _CACHE["fetched_at"] = now
    return manifest


def get_latest_version() -> Optional[str]:
    """
    Return the latest worker version string, or None if there is no
    authoritative source.

    Priority:
      1. manifest.version (uploaded by release.sh)
      2. WORKER_AGENT_VERSION env var (legacy fallback)
      3. None — do NOT invent a placeholder like "1.0.0". Returning None
         means `update_available` stays False everywhere; operators must
         cut a real release before Update becomes actionable. This avoids
         the infinite self-update loop where the worker downloads a
         binary whose baked-in version matches its current version, so
         the heartbeat never clears update_requested.
    """
    m = get_manifest()
    if m and m.get("version"):
        return str(m["version"])
    env = os.getenv("WORKER_AGENT_VERSION", "").strip()
    return env or None


def is_update_available(worker_version: Optional[str]) -> bool:
    """
    True only when we have an authoritative latest version AND it differs
    from what the worker is running. Unset / unknown latest => False.
    """
    latest = get_latest_version()
    if not latest or not worker_version:
        return False
    return worker_version != latest


def get_latest_release_info() -> Dict[str, Any]:
    """Full release metadata for the Dashboard — always returns a dict."""
    m = get_manifest() or {}
    return {
        "version": m.get("version") or get_latest_version(),
        "released_at": m.get("released_at"),
        "changelog_url": m.get("changelog_url"),
        "amd64_sha256": m.get("amd64_sha256"),
        "arm64_sha256": m.get("arm64_sha256"),
        "notes": m.get("notes"),
        "source": "manifest" if m else "env",
    }
