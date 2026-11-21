"""δ Phase 2 — GitHub REST API wrapper for search + metadata fetch.

Two operations:
- ``search_repos(query)`` — Phase 6 / describe-source candidate
  generation. Hits ``/search/repositories`` with a license filter
  built from license_allowlist.ALLOWED_LICENSES and stars-desc
  ordering.
- ``fetch_repo_metadata(url)`` — Phase 11 / repo-source. Resolves
  a user-pasted git URL to a normalised metadata dict so
  repo_safety.evaluate_repo can run on it.

Both call the platform-tenant ``github_token`` secret (loaded by
the caller) for auth. Without a token they fall back to
unauthenticated requests, which share a 60 req/h pool with every
other unauthenticated caller from the same outbound IP — fine for
dev, hostile in prod. The caller signals this by passing
``token=None``; we never read env vars directly here.

Errors are mapped to typed exceptions so the API layer can return
a sensible status:
- ``NotConfigured``: token missing AND we hit a rate-limit /
  authentication-required response. Returned as 503 by the route.
- ``RateLimited``: documented 60/5000 per hour exceeded.
  Returned as 429.
- ``GitHubError``: anything else (network, parse, 5xx).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import requests

from utils import license_allowlist

logger = logging.getLogger(__name__)


GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 10  # seconds — GitHub's API is fast; a slow response is a fail


class GitHubError(Exception):
    """Base for github_search errors."""


class NotConfigured(GitHubError):
    """Hit when the platform GitHub token isn't set and an
    unauthenticated request was rate-limited or rejected."""


class RateLimited(GitHubError):
    """The API returned a documented rate-limit response. The
    caller should fall back to 'build from scratch' mode and tell
    the user to retry later."""


@dataclass
class RepoCandidate:
    full_name: str           # owner/repo
    html_url: str            # https://github.com/owner/repo
    description: str
    license_spdx: Optional[str]
    stars: int
    last_pushed_iso: str
    default_branch: str
    primary_language: Optional[str]
    archived: bool = False
    disabled: bool = False
    is_template: bool = False
    size: int = 0

    def metadata(self) -> Dict[str, Any]:
        """Shape repo_safety.evaluate_repo expects."""
        return {
            "license_spdx": self.license_spdx,
            "archived": self.archived,
            "disabled": self.disabled,
            "description": self.description,
            "stars": self.stars,
            "last_pushed_iso": self.last_pushed_iso,
            "is_template": self.is_template,
            "size": self.size,
        }

    def as_dict(self) -> Dict[str, Any]:
        return {
            "full_name": self.full_name,
            "html_url": self.html_url,
            "description": self.description,
            "license_spdx": self.license_spdx,
            "stars": self.stars,
            "last_pushed_iso": self.last_pushed_iso,
            "default_branch": self.default_branch,
            "primary_language": self.primary_language,
            "archived": self.archived,
            "disabled": self.disabled,
            "is_template": self.is_template,
            "size": self.size,
        }


def _build_headers(token: Optional[str]) -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mod-core-describe-and-build/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _is_rate_limit_response(resp: requests.Response) -> bool:
    """GitHub returns 403 with `X-RateLimit-Remaining: 0` when the
    secondary rate limit kicks in, OR a documented 429. Catch both."""
    if resp.status_code == 429:
        return True
    if resp.status_code == 403:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            return True
        body = (resp.text or "").lower()
        if "rate limit" in body or "abuse detection" in body:
            return True
    return False


def _build_license_filter() -> str:
    """Construct the ``license:`` qualifiers GitHub accepts.

    GitHub's search API maps SPDX → keyword via its own table; not
    all SPDX values it accepts match ours 1:1. We pick the
    intersection of "in our allowlist AND known to GitHub search"
    so we don't waste API calls on rejects.
    """
    # GitHub's documented search-license keywords as of 2025.
    # Source: docs.github.com/en/rest/search/search#search-repositories
    GITHUB_KNOWN = {
        "MIT": "mit",
        "MIT-0": "mit-0",
        "Apache-2.0": "apache-2.0",
        "BSD-2-Clause": "bsd-2-clause",
        "BSD-3-Clause": "bsd-3-clause",
        "BSD-3-Clause-Clear": "bsd-3-clause-clear",
        "ISC": "isc",
        "0BSD": "0bsd",
        "Unlicense": "unlicense",
        "CC0-1.0": "cc0-1.0",
        "MPL-2.0": "mpl-2.0",
        "Zlib": "zlib",
        "BSL-1.0": "bsl-1.0",
        "PostgreSQL": "postgresql",
    }
    keywords = [
        v for k, v in GITHUB_KNOWN.items()
        if license_allowlist.is_allowed(k)
    ]
    return " ".join(f"license:{kw}" for kw in keywords)


def search_repos(
    query: str,
    *,
    token: Optional[str] = None,
    max_n: int = 10,
    timeout: int = DEFAULT_TIMEOUT,
    requests_module=requests,
) -> List[RepoCandidate]:
    """Search GitHub repositories matching ``query`` and a licence
    qualifier built from the allowlist. Returns up to ``max_n``
    candidates ordered by stars-desc.

    The caller is responsible for tenant rate-limiting (Phase 6).
    This function is a thin wrapper over the HTTP call so it can
    be tested against fixtures without a live network.

    Raises ``RateLimited`` on rate-limit responses.
    Raises ``NotConfigured`` when no token AND rate-limited.
    Raises ``GitHubError`` on transport / parse failures.
    """
    if not query or not query.strip():
        return []
    license_filter = _build_license_filter()
    full_query = f"{query} {license_filter}"
    params = {
        "q": full_query,
        "sort": "stars",
        "order": "desc",
        "per_page": str(max(1, min(max_n, 100))),
    }
    try:
        resp = requests_module.get(
            f"{GITHUB_API}/search/repositories",
            headers=_build_headers(token),
            params=params,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise GitHubError(f"GitHub search transport failed: {e}") from e

    if _is_rate_limit_response(resp):
        if not token:
            raise NotConfigured(
                "GitHub search rate limit hit. The platform admin "
                "needs to configure a github_token in Settings → "
                "Secrets to lift the 60/h unauthenticated cap."
            )
        raise RateLimited("GitHub authenticated rate limit hit")

    if resp.status_code >= 400:
        raise GitHubError(
            f"GitHub search returned {resp.status_code}: {resp.text[:200]}"
        )

    try:
        body = resp.json()
    except ValueError as e:
        raise GitHubError(f"GitHub search returned non-JSON: {e}") from e

    items = body.get("items") or []
    return [_item_to_candidate(item) for item in items[:max_n]]


def fetch_repo_metadata(
    url: str,
    *,
    token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    requests_module=requests,
) -> Optional[RepoCandidate]:
    """Resolve a user-pasted git URL to GitHub repo metadata.

    Accepted shapes:
    - ``https://github.com/owner/repo``
    - ``https://github.com/owner/repo.git``
    - ``git@github.com:owner/repo.git``
    - ``owner/repo``

    Returns ``None`` if the URL is not GitHub-hosted (we only run
    safety checks on GitHub; non-GitHub URLs are rejected upstream
    by Phase 11).

    Raises the same errors as ``search_repos`` for rate / transport
    issues. A 404 is mapped to ``GitHubError`` with a clear "repo
    not found or private" message.
    """
    full_name = _parse_owner_repo(url)
    if full_name is None:
        return None
    try:
        resp = requests_module.get(
            f"{GITHUB_API}/repos/{full_name}",
            headers=_build_headers(token),
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise GitHubError(f"GitHub metadata transport failed: {e}") from e

    if _is_rate_limit_response(resp):
        if not token:
            raise NotConfigured(
                "GitHub metadata rate limit hit. Configure a "
                "github_token in Settings → Secrets."
            )
        raise RateLimited("GitHub authenticated rate limit hit")

    if resp.status_code == 404:
        raise GitHubError(
            f"Repository {full_name} not found, or private and "
            "the configured token lacks access."
        )
    if resp.status_code >= 400:
        raise GitHubError(
            f"GitHub returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except ValueError as e:
        raise GitHubError(f"GitHub returned non-JSON: {e}") from e
    return _item_to_candidate(body)


# Install-strategy classification. The wizard's synthesize step
# generates a Dockerfile that needs to know HOW to install the repo's
# code. Pre-flighting this avoids the failure mode where the LLM
# blindly writes `pip install git+<url>` against a repo that has no
# setup.py / pyproject.toml.
#
# Strategies (most-preferred first):
#   pip          repo has setup.py OR pyproject.toml at root
#                → Dockerfile: `pip install git+https://github.com/.../<repo>`
#   requirements repo has requirements.txt + .py files (no package
#                metadata) → clone + pip install -r requirements.txt +
#                PYTHONPATH=/app/<repo>
#   dockerfile   repo has a Dockerfile at root → use that as FROM
#   clone        only .py files in root or src/, no install metadata
#                → clone + PYTHONPATH=/app/<repo>
#   unusable     no recognisable Python entry points → drop from
#                candidates entirely

_INSTALL_STRATEGY_PIP          = "pip"
_INSTALL_STRATEGY_REQUIREMENTS = "requirements"
_INSTALL_STRATEGY_DOCKERFILE   = "dockerfile"
_INSTALL_STRATEGY_CLONE        = "clone"
_INSTALL_STRATEGY_UNUSABLE     = "unusable"


def detect_install_strategy(
    full_name: str,
    *,
    token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    requests_module=requests,
) -> str:
    """Classify how a GitHub repo should be packaged into a Docker
    image. Returns one of the _INSTALL_STRATEGY_* constants.

    On any GitHub API failure (rate-limited, transport error, 404)
    returns the safest fallback (`unusable`) so the caller filters
    the candidate out — better to drop than risk shipping a broken
    Dockerfile to the user.
    """
    try:
        resp = requests_module.get(
            f"{GITHUB_API}/repos/{full_name}/contents/",
            headers=_build_headers(token),
            timeout=timeout,
        )
    except requests.RequestException:
        return _INSTALL_STRATEGY_UNUSABLE
    if resp.status_code != 200:
        return _INSTALL_STRATEGY_UNUSABLE
    try:
        items = resp.json()
    except ValueError:
        return _INSTALL_STRATEGY_UNUSABLE
    if not isinstance(items, list):
        return _INSTALL_STRATEGY_UNUSABLE

    root_files = {
        (it.get("name") or "").lower(): (it.get("type") or "")
        for it in items
        if isinstance(it, dict) and it.get("name")
    }

    has_setup_py     = "setup.py" in root_files
    has_pyproject    = "pyproject.toml" in root_files
    has_requirements = "requirements.txt" in root_files
    has_dockerfile   = "dockerfile" in root_files
    has_py_files     = any(name.endswith(".py") for name in root_files)
    has_src_dir      = "src" in root_files and root_files["src"] == "dir"

    # Strategy precedence — most-deterministic install paths win.
    if has_setup_py or has_pyproject:
        return _INSTALL_STRATEGY_PIP
    if has_requirements and (has_py_files or has_src_dir):
        return _INSTALL_STRATEGY_REQUIREMENTS
    if has_dockerfile:
        return _INSTALL_STRATEGY_DOCKERFILE
    if has_py_files or has_src_dir:
        return _INSTALL_STRATEGY_CLONE
    return _INSTALL_STRATEGY_UNUSABLE


def _parse_owner_repo(url: str) -> Optional[str]:
    """Extract ``owner/repo`` from one of the accepted URL shapes.
    Returns None for non-GitHub URLs."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    # owner/repo shorthand
    if "/" in url and "://" not in url and "@" not in url and " " not in url:
        parts = url.strip("/").split("/")
        if len(parts) == 2 and all(p for p in parts):
            return f"{parts[0]}/{parts[1].removesuffix('.git')}"
    # SSH form: git@github.com:owner/repo.git
    if url.startswith("git@"):
        if not url.startswith("git@github.com:"):
            return None
        rest = url.split(":", 1)[1]
        parts = rest.strip("/").split("/")
        if len(parts) == 2:
            return f"{parts[0]}/{parts[1].removesuffix('.git')}"
        return None
    # https form
    parsed = urlparse(url)
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1].removesuffix('.git')}"
    return None


def _item_to_candidate(item: Dict[str, Any]) -> RepoCandidate:
    license_obj = item.get("license") or {}
    spdx = None
    if isinstance(license_obj, dict):
        spdx = license_obj.get("spdx_id")
        # GitHub uses "NOASSERTION" for repos with a LICENSE file
        # it can't auto-detect. Treat as "no licence" for our
        # purposes — the allowlist will reject it.
        if spdx == "NOASSERTION":
            spdx = None
    return RepoCandidate(
        full_name=item.get("full_name") or "",
        html_url=item.get("html_url") or "",
        description=(item.get("description") or "")[:500],
        license_spdx=spdx,
        stars=int(item.get("stargazers_count") or 0),
        last_pushed_iso=item.get("pushed_at") or "",
        default_branch=item.get("default_branch") or "main",
        primary_language=item.get("language"),
        archived=bool(item.get("archived")),
        disabled=bool(item.get("disabled")),
        is_template=bool(item.get("is_template")),
        size=int(item.get("size") or 0),
    )
