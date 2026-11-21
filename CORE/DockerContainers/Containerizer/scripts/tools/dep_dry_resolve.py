"""Pre-flight dry-resolve for LLM-generated pip dependencies (issue #2b).

The LLM occasionally writes nonexistent versions into ``pip install``
lines — most famously ``gradio==6.0.1`` which doesn't exist on PyPI
(gradio is at the v4/v5 era). The build then fails 4 minutes in
when ``docker build`` tries to install it. This module catches
those before the build dispatches: it runs ``pip install --dry-run``
against PyPI (and any caller-supplied ``--extra-index-url``) for
each package extracted from the Dockerfile and reports which ones
won't resolve.

Pure subprocess wrapper. Uses an in-memory cache keyed by
``(package_spec, index_urls)`` so repeat resolutions within one
Containerizer task avoid hammering PyPI for the same package.

Apt dry-resolve is intentionally NOT included here — it needs a
sidecar Docker container with the FROM image as base, which the
Containerizer doesn't have access to. Apt failures are caught at
ImageBuilder time via the BUILD_RESULT_JSON envelope (error_class
\"apt_install_failed\") and the user can use Retry with corrections.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

# In-process cache. Keyed by (package_spec, frozenset(index_urls)).
# Don't bother with TTL: the cache lives only for the lifetime of
# the Containerizer task (~30s-2min) and the LLM doesn't re-emit
# the same dep within one task often enough to need TTL pruning.
_CACHE: Dict[Tuple[str, frozenset], Tuple[bool, Optional[str]]] = {}


# Reuse split_run_heuristic's pip-line parser so we extract pip
# tokens consistently with how the splitter sees them. Avoids
# drift between "what we split on" and "what we dry-resolve".
def _extract_pip_lines(dockerfile_text: str) -> List[Tuple[List[str], List[str]]]:
    """Walk a Dockerfile (already coalesced or not) and return one
    ``(flags, packages)`` tuple per ``RUN pip install`` line.
    """
    from .split_run_heuristic import _LINE_CONT_RE, _parse_pip_install_line  # type: ignore[attr-defined]

    coalesced = _LINE_CONT_RE.sub(" ", dockerfile_text)
    out: List[Tuple[List[str], List[str]]] = []
    for line in coalesced.split("\n"):
        parsed = _parse_pip_install_line(line)
        if parsed is not None:
            out.append(parsed)
    return out


def _pip_index_args(flags: List[str]) -> List[str]:
    """Subset the flag list to just the index-related flags. The
    dry-resolve subprocess inherits these so the resolution
    happens against the SAME index the build would use (e.g. the
    Intel XPU wheel index for torch+xpu).
    """
    out: List[str] = []
    i = 0
    while i < len(flags):
        f = flags[i]
        if f in ("--index-url", "--extra-index-url", "--find-links", "--trusted-host"):
            if i + 1 < len(flags):
                out.append(f)
                out.append(flags[i + 1])
                i += 2
                continue
        i += 1
    return out


def _pip_extras_to_skip(token: str) -> bool:
    """Heuristic: skip dry-resolution for tokens that aren't real
    package specs. Editable installs, local paths, and VCS URLs
    don't have meaningful PyPI dry-resolution behaviour.
    """
    return (
        token.startswith("-")
        or token.startswith(".")
        or token.startswith("/")
        or token.startswith("git+")
        or token.startswith("hg+")
        or token.startswith("svn+")
        or token.startswith("http://")
        or token.startswith("https://")
        or token.startswith("file://")
    )


def dry_resolve_one(
    package: str,
    *,
    index_args: Optional[List[str]] = None,
    timeout_s: int = 30,
) -> Tuple[bool, Optional[str]]:
    """Run ``pip install --dry-run`` for a single package spec.
    Returns ``(ok, error)``. ``error`` is a one-line summary
    extracted from pip's stderr on failure (truncated to ~300
    chars) or ``None`` on success.

    Uses an in-process cache so repeat calls for the same
    ``(package, index_args)`` skip the network round-trip.
    """
    key = (package, frozenset(index_args or ()))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    cmd = [
        sys.executable, "-m", "pip", "install",
        "--dry-run",
        "--quiet",
        # Don't actually fetch wheels; resolver-only. Pip's resolver
        # still hits the index for metadata but doesn't download.
        "--no-deps",
        "--disable-pip-version-check",
        package,
    ]
    if index_args:
        cmd[5:5] = index_args  # insert after "--quiet"

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        out = (False, f"pip dry-resolve timed out after {timeout_s}s")
        _CACHE[key] = out
        return out
    except FileNotFoundError:
        out = (True, None)  # pip not available locally — skip
        _CACHE[key] = out
        return out

    if result.returncode == 0:
        out = (True, None)
    else:
        # Most useful single line is usually the "ERROR: Could not
        # find a version that satisfies ..." or the resolver's
        # "ResolutionImpossible" line. Grab the first one.
        msg = (result.stderr or result.stdout or "").strip()
        first_err = next(
            (l for l in msg.splitlines() if l.startswith("ERROR")),
            msg.splitlines()[0] if msg.splitlines() else "pip dry-resolve failed",
        )
        out = (False, first_err[:300])

    _CACHE[key] = out
    return out


# ---------------------------------------------------------------------------
# Apt static-index dry-resolve (issue #2d / Option A).
#
# We can't run `apt-get install --dry-run` from inside the
# Containerizer because that would need Docker socket access (root-
# equivalent on the host — see Option B which puts it where the
# socket already lives). Instead, query the published distro package
# index over HTTPS for each apt package the LLM emitted: 200 →
# package exists, 404 → it doesn't (typo, removed, wrong distro).
#
# Catches: typos, removed packages, distro-mismatch (asking for an
# Ubuntu-only package on Alpine).
#
# Misses: repo-config / repo-reachability failures inside the build
# container (those get caught at ImageBuilder time via Option B's
# pre-flight dry-run, or at real-build time via the existing
# BUILD_RESULT_JSON apt_install_failed envelope).
# ---------------------------------------------------------------------------

# Map a Docker FROM ref to (distro, suite). Suite is the codename
# the distro uses on its package-index URL — Debian uses Bookworm /
# Bullseye, Ubuntu uses Jammy / Noble, Alpine uses v3.19 etc.
# We default to "skip" (return None) for any base image we don't
# recognise; the caller treats unknown distros as "can't pre-flight"
# and falls through to ImageBuilder-side validation.
_DEBIAN_SLIM_RE = re.compile(
    r"python:(\d+(?:\.\d+)*)-(?:slim|bookworm|bullseye|buster|trixie)",
)
_DEBIAN_RE = re.compile(r"^(?:debian:|library/debian:)([a-z0-9._-]+)")
_UBUNTU_RE = re.compile(r"^(?:ubuntu:|library/ubuntu:)([a-z0-9._-]+)")
_ALPINE_RE = re.compile(r"alpine:?([0-9.]+)?")


def _identify_distro(from_ref: str) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort (distro, suite) extraction from a FROM ref.

    Returns (None, None) when we can't classify — callers should
    skip apt validation for those refs rather than guess.
    """
    ref = (from_ref or "").lower().strip()

    # python:3.X-slim is the most common — the Debian suite is implied
    # by the python tag epoch and rolls forward as Debian stables ship.
    # Current state (2026): python:3.11-slim and python:3.12-slim both
    # resolve to Debian Trixie (13). python:3.10-slim is on Bullseye.
    # python:3.9-slim is Bullseye too. We probe `cat /etc/os-release`
    # in CI to keep this honest, but the default mapping is what's
    # baked here.
    m = _DEBIAN_SLIM_RE.match(ref)
    if m:
        major_minor = m.group(1)
        try:
            major = int(major_minor.split(".")[0])
            minor = int((major_minor.split(".") + ["0"])[1])
        except ValueError:
            return ("debian", "trixie")
        if major == 3 and minor >= 11:
            return ("debian", "trixie")
        if major == 3 and minor == 10:
            return ("debian", "bullseye")
        return ("debian", "trixie")

    m = _DEBIAN_RE.match(ref)
    if m:
        return ("debian", m.group(1))
    m = _UBUNTU_RE.match(ref)
    if m:
        return ("ubuntu", m.group(1))
    m = _ALPINE_RE.match(ref)
    if m:
        return ("alpine", m.group(1) or "edge")

    # Unrecognised → caller skips
    return (None, None)


def _strip_arch_qualifier(token: str) -> str:
    """Remove the Debian/Ubuntu :i386 / :amd64 arch suffix from an
    apt package name. The package-index URL doesn't include the
    arch qualifier; ``libc6:i386`` and ``libc6`` resolve to the
    same page.
    """
    if ":" in token and not token.startswith("-"):
        # Reject if the colon is part of a version pin like ``foo=1:2.3``
        # which apt allows. Only strip when the suffix is a known arch.
        head, tail = token.rsplit(":", 1)
        if tail in {"i386", "amd64", "arm64", "armhf", "armel", "ppc64el", "s390x", "any", "native"}:
            return head
    return token


def _apt_index_url(distro: str, suite: str, package: str) -> Optional[str]:
    """Build the HTTPS URL for a (distro, suite, package) tuple. The
    page returns 200 if the package is in that suite's index and 404
    otherwise. Returns None for unknown distros.
    """
    pkg = _strip_arch_qualifier(package).strip()
    if not pkg:
        return None
    # pkg names allow letters / digits / `.` / `-` / `+` ; reject
    # anything else as a likely token-parsing miss.
    if not re.match(r"^[a-zA-Z0-9.+\-]+$", pkg):
        return None
    if distro == "debian":
        return f"https://packages.debian.org/{quote(suite)}/{quote(pkg)}"
    if distro == "ubuntu":
        return f"https://packages.ubuntu.com/{quote(suite)}/{quote(pkg)}"
    if distro == "alpine":
        # Alpine's pkg page lives under /package/{branch}/{repo}/x86_64/{name}.
        # Branch is `vN.NN` for releases or `edge` for current.
        branch = suite if suite.startswith("v") or suite == "edge" else f"v{suite}"
        return f"https://pkgs.alpinelinux.org/package/{quote(branch)}/main/x86_64/{quote(pkg)}"
    return None


# Apt-resolve cache keyed by (package, distro, suite). Same TTL
# scope as the pip cache: in-process for the lifetime of the
# Containerizer task.
_APT_CACHE: Dict[Tuple[str, str, str], Tuple[bool, Optional[str]]] = {}


def apt_index_check_one(
    package: str,
    *,
    distro: str,
    suite: str,
    fetch: Optional[Callable[[str], Tuple[int, str]]] = None,
    timeout_s: float = 8.0,
) -> Tuple[bool, Optional[str]]:
    """Return (ok, error) for a single apt package against the
    distro's package index. ``fetch`` is injectable for tests; in
    production we call ``urllib.request`` so we don't pull a new
    httpx dependency for what's effectively a HEAD-equivalent
    check.
    """
    pkg = _strip_arch_qualifier(package)
    key = (pkg, distro, suite)
    cached = _APT_CACHE.get(key)
    if cached is not None:
        return cached

    url = _apt_index_url(distro, suite, pkg)
    if url is None:
        out = (True, None)  # can't classify → don't block
        _APT_CACHE[key] = out
        return out

    if fetch is None:
        # Production fetch via stdlib urllib so this module doesn't
        # add a new top-level dependency. HEAD avoids downloading
        # the page body when the package exists.
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError

        def _stdlib_fetch(u: str) -> Tuple[int, str]:
            try:
                req = Request(u, method="HEAD")
                with urlopen(req, timeout=timeout_s) as resp:
                    return (resp.status, "")
            except HTTPError as e:
                return (e.code, str(e))
            except URLError as e:
                return (0, f"network error: {e}")
            except Exception as e:  # noqa: BLE001
                return (0, f"unexpected: {e}")

        fetch = _stdlib_fetch

    status, error_text = fetch(url)
    if status == 200:
        out = (True, None)
    elif status == 404:
        # Best-effort: surface a one-line "not found in {distro}/{suite}"
        # so the FE can render an informative card.
        out = (False, f"package not in {distro}/{suite} index ({url})")
    elif status == 0:
        # Network error — don't block; treat as "couldn't verify".
        out = (True, None)
    else:
        # Unexpected status — log but don't block.
        out = (True, None)

    _APT_CACHE[key] = out
    return out


def _extract_apt_lines(dockerfile_text: str) -> List[Tuple[List[str], List[str]]]:
    """Parallel of ``_extract_pip_lines`` — returns one (flags,
    packages) tuple per ``RUN apt-get install`` line. Skips lines
    that mix `apt-get update && apt-get install` because the parser
    in split_run_heuristic only handles bare apt-install lines.
    """
    from .split_run_heuristic import _LINE_CONT_RE, _parse_apt_install_line  # type: ignore[attr-defined]

    coalesced = _LINE_CONT_RE.sub(" ", dockerfile_text)
    out: List[Tuple[List[str], List[str]]] = []
    for line in coalesced.split("\n"):
        parsed = _parse_apt_install_line(line)
        if parsed is not None:
            flags, packages, _trailing = parsed
            out.append((flags, packages))
    return out


def dry_resolve_dockerfile_apt(
    dockerfile_text: str,
    *,
    from_ref: Optional[str] = None,
    fetch: Optional[Callable[[str], Tuple[int, str]]] = None,
    timeout_s: float = 8.0,
) -> List[Dict[str, str]]:
    """Top-level entry: extract every apt-install package from a
    Dockerfile, classify the distro from the FROM ref, and return
    a list of FAILED entries shaped for ``BUILD_RESULT_JSON``
    envelopes.

    Returns an empty list when the FROM image isn't a recognised
    apt-using distro (Alpine without apt, distroless, scratch,
    etc.) — apt-using vs apt-less is detected by ``_identify_distro``
    returning ``(None, None)``.
    """
    # FROM detection: prefer the caller-supplied ref. Fall back to
    # parsing it out of the Dockerfile text if absent.
    if from_ref is None:
        m = re.search(r"^\s*FROM\s+(\S+)", dockerfile_text or "", re.MULTILINE)
        from_ref = m.group(1) if m else ""

    distro, suite = _identify_distro(from_ref)
    if distro is None or suite is None:
        return []

    apt_lines = _extract_apt_lines(dockerfile_text)
    failures: List[Dict[str, str]] = []
    for flags, packages in apt_lines:
        for pkg in packages:
            if pkg.startswith("-"):
                continue
            ok, err = apt_index_check_one(
                pkg,
                distro=distro,
                suite=suite,
                fetch=fetch,
                timeout_s=timeout_s,
            )
            if not ok and err is not None:
                failures.append({
                    "package": pkg,
                    "command": f"apt-get install {' '.join(flags)} {pkg}".strip(),
                    "error": err,
                    "distro": distro,
                    "suite": suite,
                })
    return failures


def dry_resolve_dockerfile_pips(
    dockerfile_text: str,
    *,
    timeout_per_pkg_s: int = 30,
) -> List[Dict[str, str]]:
    """Top-level entry: extract every pip-install package from a
    Dockerfile, dry-resolve each, and return a list of FAILED
    entries shaped for direct use as ``BUILD_RESULT_JSON`` envelopes
    (variant + dockerfile fields are filled by the caller since
    this module doesn't know which variant the Dockerfile belongs
    to).

    Each entry:
        {
            "package": "gradio==6.0.1",
            "command": "pip install --no-cache-dir gradio==6.0.1",
            "error": "ERROR: Could not find ...",
        }
    """
    pip_lines = _extract_pip_lines(dockerfile_text)
    failures: List[Dict[str, str]] = []
    for flags, packages in pip_lines:
        index_args = _pip_index_args(flags)
        for pkg in packages:
            if _pip_extras_to_skip(pkg):
                continue
            ok, err = dry_resolve_one(
                pkg,
                index_args=index_args,
                timeout_s=timeout_per_pkg_s,
            )
            if not ok and err is not None:
                failures.append({
                    "package": pkg,
                    "command": f"pip install {' '.join(flags)} {pkg}".strip(),
                    "error": err,
                })
    return failures
