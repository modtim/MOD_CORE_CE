"""Thin client for the build-remediation cache, used by the wizard's
test-and-repair loop.

The cache (BuildRemediation + qdrant) is platform-wide knowledge that
records which Dockerfile fixes resolved which build errors, so a later
build hitting the same error gets the fix without re-running the LLM
repair loop. The API endpoints live on the node_definitions router:

    GET  /v1/nodes/remediation/lookup
    POST /v1/nodes/remediation/record
    POST /v1/nodes/remediation/record-failed-attempt

This module is the wizard-side counterpart to ImageBuilder's
``llm_remediation.py``. The ``normalise_error`` / ``error_signature`` /
``diff_hash`` functions are COPIED VERBATIM from that module on purpose
— both build paths must produce identical signatures so they share
cache entries. KEEP IN SYNC with
``DockerContainers/ImageBuilder/llm_remediation.py``.

Every network call is fail-open: a cache outage must never block or
fail a build. Lookups return ``None``; records silently no-op.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_TIMEOUT_S = 10


# --- signature (KEEP IN SYNC with ImageBuilder/llm_remediation.py) ---------

_NORMALISE_PATTERNS = [
    (re.compile(r"\bsha256:[0-9a-f]{6,64}\b"), "sha256:<HASH>"),
    (re.compile(r"/var/lib/containerd/[^\s\"']+"), "<containerd-path>"),
    (re.compile(r"/tmp/pip-(?:build|req|wheel)-[A-Za-z0-9_-]+"), "/tmp/pip-<build-dir>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\bpid[=: ]\d+"), "pid=<pid>"),
    (re.compile(r"\b\d{4,}\b"), "<num>"),
]


def normalise_error(stderr_tail: str) -> str:
    if not stderr_tail:
        return ""
    out = stderr_tail
    for pat, repl in _NORMALISE_PATTERNS:
        out = pat.sub(repl, out)
    out = re.sub(r"\s+", " ", out).strip()
    return out[-2000:]


def error_signature(stderr_tail: str) -> str:
    return hashlib.sha256(normalise_error(stderr_tail).encode("utf-8")).hexdigest()


def diff_hash(diff: str) -> str:
    return hashlib.sha256((diff or "").encode("utf-8")).hexdigest()


# --- HTTP wrappers (all fail-open) -----------------------------------------

def lookup(api_url: str, api_key: str, sig: str, error_text: str) -> Optional[dict]:
    """Return the lookup response dict (diff, diff_hash, promoted, trust,
    known_bad, ...) or None on any miss/error."""
    if not sig:
        return None
    try:
        import urllib.parse
        url = (
            f"{api_url.rstrip('/')}/v1/nodes/remediation/lookup?"
            f"error_signature={urllib.parse.quote(sig)}"
            f"&error_text={urllib.parse.quote((error_text or '')[:1500])}"
        )
        r = requests.get(url, headers={"X-API-Key": api_key}, timeout=_TIMEOUT_S)
        if r.ok:
            return r.json()
    except Exception as e:  # noqa: BLE001
        logger.info("remediation lookup failed (non-fatal): %s", e)
    return None


def record_success(
    api_url: str, api_key: str, sig: str, diff: str, error_text: str,
) -> None:
    """Record a Dockerfile that converged for this error signature."""
    if not sig or not diff:
        return
    try:
        requests.post(
            f"{api_url.rstrip('/')}/v1/nodes/remediation/record",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json={
                "error_signature": sig,
                "error_sample": (error_text or "")[:2000] or None,
                "error_text": normalise_error(error_text)[-2000:],
                "diff": diff,
                "diff_hash": diff_hash(diff),
            },
            timeout=_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001
        logger.info("remediation record failed (non-fatal): %s", e)


def record_failed_attempt(
    api_url: str, api_key: str, sig: str, diff: str, error_text: str,
) -> None:
    """Remember a Dockerfile that was tried for this error and failed, so
    future builds (and the repair LLM) don't retry it."""
    if not sig or not diff:
        return
    try:
        requests.post(
            f"{api_url.rstrip('/')}/v1/nodes/remediation/record-failed-attempt",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json={
                "error_signature": sig,
                "error_sample": (error_text or "")[:2000] or None,
                "diff": diff,
                "diff_hash": diff_hash(diff),
            },
            timeout=_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001
        logger.info("remediation record-failed-attempt failed (non-fatal): %s", e)


def known_bad_excerpts(lookup_resp: Optional[dict]) -> List[str]:
    """Pull the known_bad excerpts out of a lookup response into a flat
    list of strings for the repair prompt."""
    if not lookup_resp:
        return []
    out: List[str] = []
    for kb in (lookup_resp.get("known_bad") or [])[:10]:
        if isinstance(kb, dict):
            excerpt = (kb.get("excerpt") or "").strip()
            if excerpt:
                out.append(excerpt)
    return out
