"""Sample a Git repo's "shape" via raw HTTPS fetches — no `git clone`.

Used by the New Node Wizard's repo mode so the LLM can analyse a repo's
real I/O contract instead of guessing from a bare URL. We pull a small
set of sentinel files (README, manifests, likely entrypoints) and
concatenate them into a single ~12 KB blob the existing
`/analyze-script` prompt accepts as `source_type="repo"`.

Why not git clone? The API container doesn't ship `git`, and adding it
+ shallow-cloning random user URLs introduces an exec-spawn-as-tenant
surface we don't need. Raw-content HTTPS fetches are sandboxed by the
Python `requests` client and capped per-file.

Supported hosts:
  - github.com               → raw.githubusercontent.com
  - raw.githubusercontent.com (already raw — pass through)
  - gitlab.com               → /-/raw/{ref}/{path}

Authenticated repos:
  Pass an `auth_token` (Bearer / Personal Access Token). The fetcher
  attaches `Authorization: Bearer <token>` to every request. We don't
  enforce origin per provider — callers are expected to pass tokens
  that match the URL's host.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests

from utils import safe_http

logger = logging.getLogger(__name__)


# Files we sample, in priority order. We stop appending once the
# concatenated body would exceed MAX_BODY_BYTES — top entries are most
# likely to characterise the repo so they get first dibs.
SENTINEL_FILES: List[str] = [
    # Project overview — highest signal.
    "README.md",
    "README.rst",
    "README",
    # Install / usage / setup docs — wizard-rebuild §3a foundation
    # for docs-driven extraction.
    "INSTALL.md",
    "INSTALL",
    "install.md",
    "CONTRIBUTING.md",
    "HOWTO.md",
    "USAGE.md",
    "docs/install.md",
    "docs/installation.md",
    "docs/getting-started.md",
    "docs/quickstart.md",
    "docs/setup.md",
    "docs/index.md",
    "examples/README.md",
    # Build / orchestration metadata — chunkable, often holds
    # invocation recipes.
    "Makefile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    # Env var manifests — explicit config surface area.
    ".env.example",
    ".env.template",
    "env.example",
    # Python project manifests.
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "environment.yml",
    "pixi.toml",
    # Other language manifests.
    "package.json",
    # Dockerfiles.
    "Dockerfile",
    "Dockerfile.cpu",
    "Dockerfile.cuda",
    # Likely entrypoints.
    "main.py",
    "app.py",
    "run.py",
    "src/main.py",
    "src/app.py",
    "src/index.ts",
    "src/index.js",
]

DEFAULT_BRANCHES: Tuple[str, ...] = ("main", "master", "develop", "trunk")

# Hard caps so a malicious URL can't slow us to a crawl.
MAX_BODY_BYTES = 12_000           # what build_user_prompt clamps to anyway
MAX_FILE_BYTES = 8_192            # per-file cap before truncation
PER_FILE_TIMEOUT_S = 6            # raw-content fetch timeout
TOTAL_TIMEOUT_BUDGET_S = 12       # stop sampling once exceeded


# ---------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------

_GH_HOSTS = ("github.com", "www.github.com", "raw.githubusercontent.com")
_GL_HOSTS = ("gitlab.com", "www.gitlab.com")


def _is_allowed_host(host: str, allowed_hosts: Tuple[str, ...]) -> bool:
    """Check if host is allowed using exact or subdomain match.

    Examples:
      - github.com matches github.com ✓
      - api.github.com matches github.com ✓
      - notgithub.com does not match github.com ✗
      - github.com.evil.com does not match github.com ✗
    """
    host_lower = host.lower().rstrip(".")
    for allowed in allowed_hosts:
        allowed_lower = allowed.lower().rstrip(".")
        if host_lower == allowed_lower or host_lower.endswith("." + allowed_lower):
            return True
    return False


def _normalize_repo_url(url: str) -> Optional[Tuple[str, str, str]]:
    """Return (provider, owner, repo) or None if we can't parse it.

    Handles common shapes:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - git@github.com:owner/repo.git           (ssh shorthand)
      - https://github.com/owner/repo/tree/main (branch URL — strip)
    """
    if not url:
        return None
    s = url.strip()
    # SSH shorthand: git@github.com:owner/repo.git
    m = re.match(r"^git@(?P<host>[^:]+):(?P<path>[^\s]+)$", s)
    if m:
        host = m.group("host"); path = m.group("path")
        s = f"https://{host}/{path}"

    parsed = urlparse(s)
    host = (parsed.netloc or "").lower()
    if not host:
        return None

    # Strip leading slash + .git suffix; split into segments.
    parts = [p for p in (parsed.path or "").strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    if not owner or not repo:
        return None

    if _is_allowed_host(host, _GH_HOSTS):
        return ("github", owner, repo)
    if _is_allowed_host(host, _GL_HOSTS):
        return ("gitlab", owner, repo)
    return None


def _raw_url(provider: str, owner: str, repo: str, ref: str, path: str) -> str:
    if provider == "github":
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    if provider == "gitlab":
        return f"https://gitlab.com/{owner}/{repo}/-/raw/{ref}/{path}"
    raise ValueError(f"unsupported provider: {provider}")


def _resolve_default_branch(provider: str, owner: str, repo: str,
                              auth_token: Optional[str]) -> Optional[str]:
    """GitHub + GitLab both expose a JSON API that names the default
    branch. We hit that once before sampling files so we don't 404 on
    repos whose default branch isn't `main`."""
    headers = {"Accept": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    try:
        if provider == "github":
            url = f"https://api.github.com/repos/{owner}/{repo}"
        elif provider == "gitlab":
            url = f"https://gitlab.com/api/v4/projects/{owner}%2F{repo}"
        else:
            return None
        resp = safe_http.get(url, headers=headers,
                             _ssrf_connect_timeout=PER_FILE_TIMEOUT_S,
                             _ssrf_read_timeout=PER_FILE_TIMEOUT_S)
        if not resp.ok:
            return None
        data = resp.json()
        # GitHub: default_branch; GitLab: default_branch
        return data.get("default_branch")
    except Exception as e:
        logger.debug("default-branch resolution failed: %s", e)
        return None


# ---------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------

def fetch_repo_files(
    repo_url: str,
    *,
    auth_token: Optional[str] = None,
    files: Optional[List[str]] = None,
    max_body_bytes: int = MAX_BODY_BYTES,
    ref_override: Optional[str] = None,
) -> Tuple[dict[str, str], List[str], List[str], Optional[str]]:
    """Pull sentinel files from a public (or token-authenticated) Git
    repo as a per-file dict.

    Variant of `fetch_repo_snippet` that returns the structure the
    §3a/c chunker pipeline wants: `dict[path, content]` so each file
    can be chunked separately, plus a list of files that were tried
    but didn't exist (surfaces in View Plan UI).

    Args:
      ref_override: explicit ref (branch / tag / sha) to fetch. If
        None, resolves the repo's default branch.

    Returns:
      (files_by_path, files_included, files_missing, resolved_ref)
        files_by_path: `{path: content_str}` for files that were
          fetched OK.
        files_included: same paths in priority order — order
          matters for cap exhaustion semantics.
        files_missing: paths the fetcher tried but couldn't find
          (404 / connection error / empty body).
        resolved_ref: the branch / sha actually used (None if the
          URL was unsupported).

    Returns `({}, [], [], None)` on unsupported URL — caller surfaces
    "couldn't sample" rather than raising.
    """
    parsed = _normalize_repo_url(repo_url)
    if parsed is None:
        logger.info("repo_fetcher: unsupported URL shape: %r", repo_url)
        return ({}, [], [], None)
    # GSEC FX.19 R-042 / GY.C8 — wizard internet lookups refused (sealed
    # mode or egress.wizard_internet_enabled False): no outbound call.
    from utils import egress_policy
    if egress_policy.refuse_reason(egress_policy.CATEGORY_WIZARD_INTERNET):
        logger.info("repo_fetcher: fetch_repo_files refused (egress disabled)")
        return ({}, [], [], None)
    provider, owner, repo = parsed

    headers = {"Accept": "*/*"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    if ref_override:
        branches_to_try = [ref_override]
        resolved_ref: Optional[str] = ref_override
    else:
        branch = _resolve_default_branch(provider, owner, repo, auth_token)
        branches_to_try = [branch] if branch else list(DEFAULT_BRANCHES)
        resolved_ref = branch

    candidates = files or SENTINEL_FILES
    files_by_path: dict[str, str] = {}
    files_included: List[str] = []
    files_missing: List[str] = []
    used_bytes = 0

    import time
    started = time.monotonic()

    for path in candidates:
        if used_bytes >= max_body_bytes:
            files_missing.append(path)
            continue
        if (time.monotonic() - started) > TOTAL_TIMEOUT_BUDGET_S:
            logger.info("repo_fetcher: total budget exceeded after %d files", len(files_included))
            files_missing.append(path)
            continue

        content: Optional[str] = None
        winning_ref: Optional[str] = None
        for ref in branches_to_try:
            if not ref:
                continue
            url = _raw_url(provider, owner, repo, ref, path)
            try:
                resp = safe_http.get(url, headers=headers,
                                 _ssrf_connect_timeout=PER_FILE_TIMEOUT_S,
                                 _ssrf_read_timeout=PER_FILE_TIMEOUT_S)
            except Exception as e:
                logger.debug("repo_fetcher: %s @ %s failed: %s", path, ref, e)
                continue
            if resp.status_code == 200 and resp.text.strip():
                content = resp.text
                winning_ref = ref
                break

        if content is None:
            files_missing.append(path)
            continue

        if winning_ref and not resolved_ref:
            resolved_ref = winning_ref

        if len(content) > MAX_FILE_BYTES:
            content = content[:MAX_FILE_BYTES] + (
                f"\n\n... (truncated {len(content) - MAX_FILE_BYTES} bytes)"
            )

        cost = len(path) + len(content) + 16
        if used_bytes + cost > max_body_bytes:
            # Body cap hit — record as missing rather than partial.
            files_missing.append(path)
            continue

        files_by_path[path] = content
        files_included.append(path)
        used_bytes += cost

    return (files_by_path, files_included, files_missing, resolved_ref)


def fetch_repo_snippet(
    repo_url: str,
    *,
    auth_token: Optional[str] = None,
    files: Optional[List[str]] = None,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> Tuple[str, List[str]]:
    """Pull a representative slice of a public (or token-authenticated)
    Git repo over HTTPS.

    Returns (concatenated_body, files_included). If the URL is
    unsupported or all fetches fail, returns ("", []) — the caller is
    expected to surface a clear "couldn't sample" message rather than
    a hard error, so the wizard keeps moving.
    """
    parsed = _normalize_repo_url(repo_url)
    if parsed is None:
        logger.info("repo_fetcher: unsupported URL shape: %r", repo_url)
        return ("", [])
    # GSEC FX.19 R-042 / GY.C8 — wizard internet lookups refused (sealed
    # mode or egress.wizard_internet_enabled False): no outbound call.
    from utils import egress_policy
    if egress_policy.refuse_reason(egress_policy.CATEGORY_WIZARD_INTERNET):
        logger.info("repo_fetcher: fetch_repo_snippet refused (egress disabled)")
        return ("", [])
    provider, owner, repo = parsed

    headers = {"Accept": "*/*"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    branch = _resolve_default_branch(provider, owner, repo, auth_token)
    branches_to_try = [branch] if branch else list(DEFAULT_BRANCHES)

    candidates = files or SENTINEL_FILES
    body_chunks: List[str] = []
    included: List[str] = []
    used_bytes = 0

    import time
    started = time.monotonic()

    for path in candidates:
        if used_bytes >= max_body_bytes:
            break
        if (time.monotonic() - started) > TOTAL_TIMEOUT_BUDGET_S:
            logger.info("repo_fetcher: total budget exceeded after %d files", len(included))
            break

        content = None
        for ref in branches_to_try:
            if not ref:
                continue
            url = _raw_url(provider, owner, repo, ref, path)
            try:
                resp = safe_http.get(url, headers=headers,
                                 _ssrf_connect_timeout=PER_FILE_TIMEOUT_S,
                                 _ssrf_read_timeout=PER_FILE_TIMEOUT_S)
            except Exception as e:
                logger.debug("repo_fetcher: %s @ %s failed: %s", path, ref, e)
                continue
            if resp.status_code == 200 and resp.text.strip():
                content = resp.text
                break  # only try first branch that has the file

        if content is None:
            continue

        if len(content) > MAX_FILE_BYTES:
            content = content[:MAX_FILE_BYTES] + (
                f"\n\n... (truncated {len(content) - MAX_FILE_BYTES} bytes)"
            )

        chunk = f"\n\n=== {path} ===\n{content}"
        if used_bytes + len(chunk) > max_body_bytes:
            # Squeeze in a partial chunk if a non-trivial amount fits.
            slack = max_body_bytes - used_bytes
            if slack >= 256:
                body_chunks.append(chunk[:slack])
                included.append(f"{path} (partial)")
                used_bytes += slack
            break

        body_chunks.append(chunk)
        included.append(path)
        used_bytes += len(chunk)

    return ("".join(body_chunks).strip(), included)
