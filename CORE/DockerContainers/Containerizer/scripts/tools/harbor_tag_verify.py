"""Harbor tag verification for LLM-generated Dockerfiles (issue #2a).

The Containerizer's LLM has been observed inventing base-image tags
(`modtim/base-cuda:22.04`, `modtim/base-cuda:12.4.1-cudnn8-...`)
that don't exist in the tenant's Harbor. The build then fails 4
minutes in with `failed to resolve source metadata for ... not
found`, and the user has no way to know up front that the
Dockerfile was already doomed.

This module verifies every ``FROM`` directive against Harbor's
manifest endpoint BEFORE the Containerizer's task returns its
artifacts. On a 404 response the FROM ref is reported as
hallucinated so the FE can render a per-variant failure card with
``error_class: "from_image_not_found"`` and the user can click
Retry with corrections (which re-prompts the LLM with the actual
Harbor catalogue — see node-build-reliability spec).

Pure-function URL/response logic; HTTP I/O is parameterised on a
caller-supplied ``fetch`` callable so the module can be unit-tested
without hitting a real Harbor.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

# Match an image ref into (registry, repo, tag). Refs without a
# registry (e.g. `python:3.11-slim`) are treated as Docker Hub and
# skipped — Harbor verification only makes sense against the
# tenant's Harbor registry.
_FROM_RE = re.compile(
    r"^\s*FROM\s+(?P<ref>[^\s#]+)",
    re.MULTILINE,
)


def parse_from_refs(dockerfile_text: str) -> List[str]:
    """Extract every ``FROM <ref>`` value from a Dockerfile. The
    leading ``FROM`` keyword and any trailing ``AS <stage>`` are
    stripped; comments are ignored. Order matches the Dockerfile.
    """
    out: List[str] = []
    if not dockerfile_text:
        return out
    for m in _FROM_RE.finditer(dockerfile_text):
        ref = m.group("ref")
        # Strip a multi-stage `AS <name>` suffix, leaving just the image.
        if " AS " in ref or " as " in ref:
            ref = ref.split(" ", 1)[0]
        out.append(ref)
    return out


def split_image_ref(ref: str, default_registry: str = "ghcr.io") -> Tuple[str, str, str]:
    """Split an image ref into (registry, repo, tag).

    A ref like ``registry.example.com/proj/image:1.0`` →
    (``registry.example.com``, ``proj/image``, ``1.0``). A ref
    without an explicit registry (Docker Hub style like
    ``python:3.11``) gets ``default_registry`` prepended so callers
    can decide whether to verify it.
    """
    # Detect registry: a registry component is the part before the
    # first ``/`` IF it contains a ``.`` (domain) or ``:`` (port).
    registry = default_registry
    rest = ref
    if "/" in ref:
        head, tail = ref.split("/", 1)
        if "." in head or ":" in head or head == "localhost":
            registry = head
            rest = tail

    # Tag: split on the LAST ``:``, but only if the colon-suffix
    # doesn't contain a slash (which would mean it's a port number
    # in the registry component, not a tag).
    if ":" in rest and "/" not in rest.split(":", 1)[1]:
        repo, tag = rest.rsplit(":", 1)
    else:
        repo = rest
        tag = "latest"

    return registry, repo, tag


def manifest_url(registry: str, repo: str, tag: str) -> str:
    """Build the Harbor v2 manifest URL for a (repo, tag). Used for
    HEAD/GET requests to verify existence.
    """
    # quote each path segment individually so a `:` in a tag (rare
    # but valid for some digest forms) doesn't get mis-encoded as
    # the registry-port separator.
    return f"https://{registry}/v2/{quote(repo, safe='/')}/manifests/{quote(tag, safe='')}"


def tags_list_url(registry: str, repo: str) -> str:
    """Build the Harbor v2 tags-list URL for a repo. Used to fetch
    the available-tag catalogue when verification reports a 404 so
    we can include it in the retry-with-corrections prompt.
    """
    return f"https://{registry}/v2/{quote(repo, safe='/')}/tags/list"


# ---------------------------------------------------------------------------
# HTTP wrapper — caller-injectable for testability.
# ---------------------------------------------------------------------------

class FetchResponse:
    """Minimal response shape the verifier needs from the fetch
    callable. Anything that exposes ``status_code: int`` and
    optionally ``json()`` works — including httpx.Response, requests,
    or a test stub.
    """
    status_code: int
    def json(self) -> object: ...


FetchFn = Callable[[str, Dict[str, str]], FetchResponse]


def verify_from_refs(
    dockerfile_text: str,
    *,
    harbor_registry: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    fetch: Optional[FetchFn] = None,
    only_registry: Optional[str] = None,
) -> Dict[str, object]:
    """Top-level entry point. Parses every FROM in the Dockerfile,
    verifies each against Harbor's manifest endpoint, and returns:

    .. code-block:: python

        {
            "checked": [(ref, registry, repo, tag), ...],   # everything we attempted
            "missing": [(ref, registry, repo, tag), ...],   # 404s
            "skipped": [(ref, reason), ...],                # not on harbor / no creds / unreachable
            "available_tags": {repo: [tag, ...]},           # for missing repos
        }

    ``only_registry`` (default = ``harbor_registry``) gates which
    refs we even attempt to verify. Refs pointing at Docker Hub or
    a different registry are skipped — the verifier exists only to
    catch hallucinated tenant-Harbor refs.

    Pass a ``fetch`` callable for tests. In production, callers
    inject an httpx-backed fetch closure.
    """
    target_registry = only_registry or harbor_registry
    refs = parse_from_refs(dockerfile_text)

    checked: List[Tuple[str, str, str, str]] = []
    missing: List[Tuple[str, str, str, str]] = []
    skipped: List[Tuple[str, str]] = []
    available_tags: Dict[str, List[str]] = {}

    if not refs:
        return {
            "checked": checked,
            "missing": missing,
            "skipped": skipped,
            "available_tags": available_tags,
        }

    if fetch is None:
        skipped.extend((r, "no fetch wired") for r in refs)
        return {
            "checked": checked,
            "missing": missing,
            "skipped": skipped,
            "available_tags": available_tags,
        }

    auth_header: Dict[str, str] = {}
    if username and password:
        import base64
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        auth_header["Authorization"] = f"Basic {token}"

    for ref in refs:
        registry, repo, tag = split_image_ref(ref, default_registry=harbor_registry)
        if registry != target_registry:
            skipped.append((ref, f"registry {registry} != target {target_registry}"))
            continue

        url = manifest_url(registry, repo, tag)
        try:
            resp = fetch(url, auth_header)
        except Exception as e:  # noqa: BLE001
            skipped.append((ref, f"transport error: {type(e).__name__}: {e}"))
            continue

        status = getattr(resp, "status_code", 0)
        checked.append((ref, registry, repo, tag))
        if status == 404:
            missing.append((ref, registry, repo, tag))
            # Best-effort: fetch the tags list so the retry prompt can
            # include the actual catalogue. Failures here just mean
            # the prompt won't be class-aware — not fatal.
            try:
                tl_resp = fetch(tags_list_url(registry, repo), auth_header)
                if getattr(tl_resp, "status_code", 0) == 200:
                    body = tl_resp.json()
                    if isinstance(body, dict) and isinstance(body.get("tags"), list):
                        available_tags[repo] = [str(t) for t in body["tags"]]
            except Exception:
                pass
        elif 200 <= status < 300:
            # Verified.
            pass
        else:
            # 401/403/5xx: don't BLOCK — the build might still succeed
            # (e.g. transient Harbor glitch). Mark as skipped so the
            # caller knows we couldn't verify. The build attempt itself
            # remains the authoritative signal.
            skipped.append((ref, f"manifest fetch returned {status}"))

    return {
        "checked": checked,
        "missing": missing,
        "skipped": skipped,
        "available_tags": available_tags,
    }


# Docker Registry V2 / OCI Distribution spec requires manifest GETs to
# advertise which manifest schemas the caller understands via the
# `Accept` header. Without it, Harbor (Docker Distribution v2.8+)
# returns 404 even when the tag legitimately exists — confirmed on
# stage where /v2/<repo>/manifests/<tag> returned 404 but
# /v2/<repo>/tags/list/ confirmed the tag was present. The four media
# types below cover both Docker Schema 2 and OCI image manifests
# (including the multi-arch index variants), matching what `docker
# pull` itself sends. Tags-list endpoint is plain JSON and does NOT
# need this header.
_MANIFEST_ACCEPT_HEADER = ", ".join((
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
))


def httpx_fetch_factory(timeout_s: float = 10.0):
    """Production-side fetch callable backed by httpx. Imported
    lazily so unit tests can run in environments without httpx.
    """
    import httpx
    client = httpx.Client(timeout=timeout_s, verify=True)

    def _fetch(url: str, headers: Dict[str, str]) -> FetchResponse:
        # HEAD would be more polite, but Harbor's v2 manifest
        # endpoint sometimes returns 405 for HEAD. GET is the
        # safe choice.
        merged = dict(headers)
        # Send Accept for manifest fetches. The tags-list endpoint
        # doesn't need it but the extra header is harmless there.
        # Don't clobber a caller-supplied Accept — preserves
        # test/override behavior.
        if "/manifests/" in url and "Accept" not in merged:
            merged["Accept"] = _MANIFEST_ACCEPT_HEADER
        return client.get(url, headers=merged)

    return _fetch
