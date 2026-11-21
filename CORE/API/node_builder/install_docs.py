"""Move D — RAG over project install docs.

Fetches the GitHub README for a library, extracts the
"Installation" section (or the first code block in an install-
like context), caches the excerpt on library_profiles.

This is the cold-start counterpart to the install_recipe column
(move C). Recipes are GROUND TRUTH (we've seen this exact install
work). Doc excerpts are PROJECT CLAIMS (this is what the maintainer
says you need). Recipes beat docs; docs beat nothing.

Best-effort: any failure (PyPI 404, GitHub 404, parse error) is
caught, logged, and treated as "no install docs available." The
caller continues without doc hints.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

import models
from node_builder.gpu_library_index import _normalise

logger = logging.getLogger(__name__)

_PYPI_TIMEOUT_S = 5.0
_GITHUB_TIMEOUT_S = 5.0
_DOC_REFRESH_DAYS = 90
_MAX_EXCERPT_CHARS = 3000

# README section headers we treat as install-relevant. README formats
# vary but the most common variants:
#   ## Installation
#   ## Install
#   ## Quick Start (often has install steps as first block)
#   ## Getting Started
# Section ends at the next H1/H2 of equal-or-higher level.
_SECTION_PATTERNS = (
    re.compile(r"^#{1,3}\s*install(?:ation)?\b.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^#{1,3}\s*quick\s*start\b.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^#{1,3}\s*getting\s+started\b.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^#{1,3}\s*setup\b.*$", re.IGNORECASE | re.MULTILINE),
)

# GitHub URL extraction from PyPI's home_page / project_urls.
_GITHUB_URL_RE = re.compile(
    r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git|/|$)",
    re.IGNORECASE,
)


def _derive_github_repo(pypi_meta: dict) -> Optional[tuple[str, str]]:
    """Return (owner, repo) extracted from PyPI metadata, or None."""
    info = pypi_meta.get("info") or {}
    candidates = [
        info.get("home_page") or "",
        (info.get("project_urls") or {}).get("Homepage") or "",
        (info.get("project_urls") or {}).get("Source") or "",
        (info.get("project_urls") or {}).get("Source Code") or "",
        (info.get("project_urls") or {}).get("Repository") or "",
    ]
    for c in candidates:
        m = _GITHUB_URL_RE.search(c or "")
        if m:
            return m.group(1), m.group(2)
    return None


def _wizard_internet_off() -> bool:
    """GSEC FX.19 R-042 — True when wizard internet lookups are disabled
    (egress.wizard_internet_enabled False or sealed mode)."""
    from utils import egress_policy
    return (egress_policy.refuse_reason(
        egress_policy.CATEGORY_WIZARD_INTERNET) is not None)


def _fetch_pypi_meta(package: str) -> Optional[dict]:
    if _wizard_internet_off():
        return None
    try:
        r = httpx.get(
            f"https://pypi.org/pypi/{package}/json",
            timeout=_PYPI_TIMEOUT_S,
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:  # noqa: BLE001
        logger.info("install_docs: PyPI fetch failed for %s: %s", package, e)
        return None


def _fetch_github_readme(owner: str, repo: str) -> Optional[str]:
    """Fetch the README content via GitHub's content API. Returns
    decoded markdown text or None on any failure."""
    if _wizard_internet_off():
        return None
    import os
    import base64
    headers = {"Accept": "application/vnd.github.v3+json"}
    pat = os.getenv("GITHUB_PAT", "").strip()
    if pat:
        headers["Authorization"] = f"Bearer {pat}"
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/readme",
            timeout=_GITHUB_TIMEOUT_S, headers=headers,
        )
        if r.status_code != 200:
            return None
        payload = r.json()
        encoded = payload.get("content") or ""
        if not encoded:
            return None
        return base64.b64decode(encoded.encode("ascii"), validate=False).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        logger.info("install_docs: GitHub README fetch failed for %s/%s: %s", owner, repo, e)
        return None


def extract_install_section(readme: str) -> str:
    """Extract the install section (header through next H1/H2) from
    a README. Returns "" when no install-like header is found.
    Output is capped at _MAX_EXCERPT_CHARS."""
    if not readme:
        return ""
    # Find the FIRST install-like header.
    best_start: Optional[int] = None
    for pat in _SECTION_PATTERNS:
        m = pat.search(readme)
        if m and (best_start is None or m.start() < best_start):
            best_start = m.start()
    if best_start is None:
        return ""
    # Find the next H1/H2 header after the install section start.
    rest = readme[best_start:]
    # Skip the install header line itself, then look for the next H1/H2.
    next_header = re.search(r"\n#{1,2}\s+\S", rest[1:])  # skip first char to avoid matching the install header
    if next_header:
        section = rest[: next_header.start() + 1]
    else:
        section = rest
    section = section.strip()
    if len(section) > _MAX_EXCERPT_CHARS:
        section = section[:_MAX_EXCERPT_CHARS] + "\n... [excerpt truncated]"
    return section


def fetch_and_cache_install_docs(db: Session, *, package: str) -> Optional[str]:
    """Get the install docs excerpt for ``package``. Uses the cached
    column when fresh; refreshes if older than _DOC_REFRESH_DAYS.

    Always best-effort — returns None on any failure without raising.
    """
    norm = _normalise(package)
    if not norm:
        return None
    row = db.get(models.LibraryProfile, norm)
    if row is None:
        return None

    fresh_cutoff = datetime.now(timezone.utc) - timedelta(days=_DOC_REFRESH_DAYS)
    fetched_at = row.install_docs_fetched_at
    if fetched_at is not None and fetched_at > fresh_cutoff and row.install_docs_excerpt is not None:
        return row.install_docs_excerpt or None

    meta = _fetch_pypi_meta(norm)
    if meta is None:
        return None
    owner_repo = _derive_github_repo(meta)
    if owner_repo is None:
        # Mark as fetched-but-empty to avoid re-trying every repair call.
        row.install_docs_excerpt = ""
        row.install_docs_fetched_at = datetime.now(timezone.utc)
        db.commit()
        return None
    owner, repo = owner_repo
    readme = _fetch_github_readme(owner, repo)
    excerpt = extract_install_section(readme or "")
    row.install_docs_excerpt = excerpt
    row.install_docs_fetched_at = datetime.now(timezone.utc)
    db.commit()
    return excerpt or None


def render_install_docs_for_prompt(
    db: Session,
    *,
    libraries: list[str],
    max_chars: int = 4000,
) -> str:
    """Render install doc excerpts for a set of libraries as a single
    block the repair prompt can prepend. Empty string when nothing is
    available."""
    norm_libs = [_normalise(l) for l in libraries if l]
    if not norm_libs:
        return ""
    out_parts: list[str] = []
    for lib in norm_libs:
        excerpt = fetch_and_cache_install_docs(db, package=lib)
        if not excerpt:
            continue
        out_parts.append(f"# Installation docs excerpt for {lib} (from project README):\n{excerpt}")
    if not out_parts:
        return ""
    block = "\n\n".join(out_parts)
    if len(block) > max_chars:
        block = block[:max_chars] + "\n# [doc block truncated]"
    return block
