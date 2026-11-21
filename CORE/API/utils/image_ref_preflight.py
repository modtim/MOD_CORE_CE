"""Image-ref existence preflight (wizard-mode-rebuild §2 follow-up).

Verifies a user-supplied Docker image reference actually resolves
against a public registry BEFORE the wizard submits the build.
Without this, typos and confused mental models (e.g., a HuggingFace
model path like `runwayml/stable-diffusion-v1-5` mistaken for a
Docker Hub ref) trigger the entire build → test-and-repair → LLM
thrash cycle. Stage repro 2026-05-21: Tim spent 14 minutes
watching test-and-repair invent CUDA base images because the actual
`runwayml/stable-diffusion-v1-5` ref returns 404 on Docker Hub
(it's a HuggingFace model repo, not a Docker image).

Two-stage check:

  1. parse_image_ref(ref) — split into (registry_host, namespace,
     repo, tag/digest). No network.
  2. check_exists(ref) — hit the registry's manifest API. Returns
     a structured result with exists/reason/registry-source.

Supported registries:

  - Docker Hub (default for refs with no host prefix or `docker.io`):
    uses the v2 search-and-tags API at hub.docker.com — public, no
    auth needed for public images.
  - NVIDIA NGC (`nvcr.io`): manifest API requires an NGC API key for
    private images; public images respond to anonymous manifest
    requests.
  - GitHub Container Registry (`ghcr.io`): public images accept
    anonymous manifest requests.
  - HuggingFace Inference (`huggingface.co/...`): not a Docker
    registry; we explicitly REJECT these refs with a helpful message.

  Other registries fall through with `exists=unknown` so the wizard
  can still submit (defer-to-build), just without the preflight
  guarantee.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


@dataclass
class ParsedRef:
    """Decomposed docker image reference. host_is_default=True means
    the ref had no explicit host prefix and defaults to Docker Hub."""
    registry_host: str
    namespace: str
    repo: str
    tag: str
    host_is_default: bool


@dataclass
class CheckResult:
    """Outcome of an existence preflight. `exists` is None when we
    don't know (registry not supported / network error)."""
    exists: Optional[bool]
    ref: str
    parsed: Optional[ParsedRef]
    registry_source: str
    reason: str = ""
    suggestions: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reference parsing
# ---------------------------------------------------------------------------

_REGISTRY_HOSTS = {
    "docker.io", "registry-1.docker.io", "index.docker.io",
    "nvcr.io",
    "ghcr.io",
    "quay.io",
    "gcr.io",
    "registry.gitlab.com",
}

# Hosts that the user can type but that are NOT docker registries.
# huggingface.co is the big one — users see `runwayml/stable-
# diffusion-v1-5` in their HF model list and assume it's also a
# Docker image. Surface a clear "not a docker registry" message.
_NON_REGISTRY_HOSTS = {
    "huggingface.co", "hf.co",
}


def parse_image_ref(ref: str) -> Optional[ParsedRef]:
    """Split `ref` into (host, namespace, repo, tag). Returns None on
    malformed input.

    Docker reference grammar (simplified):
        [<host>[:<port>]/]<namespace>/<repo>[:<tag>|@<digest>]
        <repo>[:<tag>]   — bare repo, defaults to docker.io/library/<repo>

    Examples:
        runwayml/stable-diffusion-v1-5
          → host=docker.io, ns=runwayml, repo=stable-diffusion-v1-5, tag=latest
        nvcr.io/nvidia/pytorch:24.01-py3
          → host=nvcr.io, ns=nvidia, repo=pytorch, tag=24.01-py3
        python:3.11-slim
          → host=docker.io, ns=library, repo=python, tag=3.11-slim
    """
    if not ref or not ref.strip():
        return None
    s = ref.strip()

    # Strip digest if present (we treat digests as opaque tags).
    if "@" in s:
        s = s.split("@", 1)[0]

    # Decide whether the first segment is a host. Rule: a ref has an
    # explicit host only if it contains a `/` AND the first segment
    # looks host-shaped (has `.`, `:port`, or is `localhost`). Bare
    # `python:3.11-slim` has no slash → not a host even though the tag
    # contains a `.`; the `python:` part is the repo, `3.11-slim` is
    # the tag.
    parts = s.split("/")
    has_host = (
        len(parts) > 1
        and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost")
    )

    if has_host:
        host = parts[0]
        path = "/".join(parts[1:])
    else:
        host = "docker.io"
        path = s

    # Tag (defaults to "latest" if absent).
    if ":" in path.split("/")[-1]:
        path_no_tag, tag = path.rsplit(":", 1)
    else:
        path_no_tag, tag = path, "latest"

    # Namespace/repo split.
    if "/" in path_no_tag:
        namespace, _, repo = path_no_tag.partition("/")
    else:
        # Bare name on docker.io → library/<repo>.
        namespace = "library"
        repo = path_no_tag

    if not repo:
        return None

    return ParsedRef(
        registry_host=host,
        namespace=namespace,
        repo=repo,
        tag=tag,
        host_is_default=(not has_host),
    )


# ---------------------------------------------------------------------------
# Per-registry checkers
# ---------------------------------------------------------------------------

# Manifest mediaTypes the build can actually use as a FROM base.
# OCI image manifests, Docker v2 image manifests, and multi-arch
# indexes/lists all work. Anything else (AI model configs, in-toto
# attestations, signed payloads) can't be FROM'd.
_BUILDABLE_MANIFEST_TYPES = (
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
)

# MediaType prefixes that are KNOWN-bad as FROM bases. We surface a
# specific reason for each so the user understands why.
_NON_IMAGE_MEDIATYPE_REASONS = {
    "application/vnd.docker.ai.model.config": (
        "This ref is a Docker AI Model artifact, NOT a container image. "
        "Docker AI Models are pulled with `docker model pull` and run "
        "via Docker Model Runner — they don't work as `FROM` bases in "
        "a Dockerfile. To use this model in a workflow node, pick a "
        "real container base (e.g. huggingface/transformers-pytorch-gpu"
        ":latest, pytorch/pytorch:2.4-cuda12.1-runtime) and have the "
        "entrypoint load the model weights at runtime via diffusers, "
        "llama.cpp, or similar."
    ),
    "application/vnd.in-toto+json": (
        "This is an in-toto attestation (build provenance metadata), "
        "not a container image."
    ),
    "application/vnd.cncf.notary": (
        "This is a Notary signature payload, not a container image."
    ),
}


def _docker_hub_anon_token(parsed: ParsedRef, timeout: float) -> Optional[str]:
    """Get an anonymous pull token for a public Docker Hub repo.
    Returns None on failure — caller should treat as "couldn't
    verify" and defer."""
    try:
        r = requests.get(
            "https://auth.docker.io/token",
            params={
                "service": "registry.docker.io",
                "scope": f"repository:{parsed.namespace}/{parsed.repo}:pull",
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("token") or None
    except (requests.RequestException, ValueError) as exc:
        logger.debug("docker hub anon token failed: %s", exc)
        return None


# Manifest list mediaTypes — multi-arch images return one of these
# instead of a direct image manifest. We pick the linux/amd64
# sub-manifest from inside.
_MANIFEST_LIST_TYPES = (
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
)


def resolve_runtime_user(image_ref: str, *, timeout: float = 6.0) -> Optional[str]:
    """Return the `Config.User` field of the image's config blob.

    For wizard-rebuild Image mode: when the base image declares a
    non-root runtime user (e.g. `vault`, `nginx`, `1000`), our
    adapter Dockerfile needs to know so it can `USER root` for the
    mkdir+chmod work and `USER <original>` at the end. Detected
    here from the registry's image config rather than guessed.

    Args:
      image_ref: the user's docker ref (e.g.
        `hashicorp/vault:2.0`).

    Returns:
      - "" / None when the base runs as root by default (or we
        couldn't determine — caller treats both as "no trailing
        USER directive needed"). We deliberately collapse the two
        cases since they produce the same Dockerfile shape.
      - A non-empty string ("vault", "10001", "nonroot", etc.)
        when the base declares a runtime user. Caller emits
        `USER <returned-value>` at the end of the Dockerfile.

    Only Docker Hub is implemented today; other registries return
    None (fallback: image runs as root). The build will still
    succeed for the common python:3.11-slim case (it's root); the
    vault / nginx / etc. fix lives behind this helper.
    """
    parsed = parse_image_ref(image_ref)
    if parsed is None:
        return None

    # Docker Hub only for now. Other registries (nvcr.io, ghcr.io, etc.)
    # have similar APIs but each needs its own auth dance; not in
    # scope for this fix.
    if parsed.registry_host not in (
        "docker.io", "registry-1.docker.io", "index.docker.io",
    ):
        return None

    token = _docker_hub_anon_token(parsed, timeout=timeout)
    if not token:
        return None

    base_url = f"https://registry-1.docker.io/v2/{parsed.namespace}/{parsed.repo}"
    accept = ", ".join((
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    ))
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
    }

    # Step 1: fetch the manifest (or manifest list).
    try:
        r = requests.get(
            f"{base_url}/manifests/{parsed.tag}",
            headers=headers, timeout=timeout,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None

    try:
        manifest = r.json()
    except ValueError:
        return None

    media_type = (r.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if not media_type:
        media_type = (manifest.get("mediaType") or "").lower()

    # Step 2: if it's a manifest list, fetch one linux child's manifest.
    # Any platform answers, since the USER directive is the same across an
    # image's platforms by Docker convention — amd64 is merely preferred.
    if media_type in _MANIFEST_LIST_TYPES:
        child_digest = _pick_inspectable_digest(manifest)
        if not child_digest:
            return None
        try:
            r = requests.get(
                f"{base_url}/manifests/{child_digest}",
                headers=headers, timeout=timeout,
            )
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        try:
            manifest = r.json()
        except ValueError:
            return None

    # Step 3: pull the config blob digest out of the manifest.
    config_digest = (
        (manifest.get("config") or {}).get("digest")
    )
    if not config_digest:
        return None

    # Step 4: fetch the config blob and read Config.User.
    try:
        r = requests.get(
            f"{base_url}/blobs/{config_digest}",
            headers=headers, timeout=timeout,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        config = r.json()
    except ValueError:
        return None

    user = (config.get("config") or {}).get("User", "")
    if not isinstance(user, str):
        return None
    user = user.strip()
    # Normalize root variants to empty — caller treats empty as
    # "no trailing USER directive needed."
    if user in ("", "root", "0"):
        return ""
    return user


def _pick_inspectable_digest(manifest_list: dict) -> Optional[str]:
    """A linux manifest whose config we can read — amd64 for preference.

    The caller wants the USER directive, which is the same on every platform
    of a given image by Docker convention, so ANY linux entry answers the
    question. amd64 stays first only because it is the most likely to be
    cached nearby.

    Previously this returned amd64 or nothing, which meant an arm64-ONLY base
    image could not be inspected at all — runtime-user detection simply failed,
    and a node built on such a base would carry no USER. That was invisible
    while every image was amd64 and stops being invisible the moment someone
    builds for a Pi.

    `unknown` platforms are skipped: buildx attaches provenance and SBOM
    records that way, and their "config" is not an image config.
    """
    fallback = None
    for entry in manifest_list.get("manifests", []) or []:
        if not isinstance(entry, dict):
            continue
        platform = entry.get("platform") or {}
        if platform.get("os") != "linux":
            continue
        arch = platform.get("architecture")
        if not arch or arch == "unknown":
            continue
        digest = entry.get("digest")
        if not (isinstance(digest, str) and digest.startswith("sha256:")):
            continue
        if arch == "amd64":
            return digest
        if fallback is None:
            fallback = digest
    return fallback


def _classify_manifest_mediatype(content_type: str) -> Optional[str]:
    """Given a manifest Content-Type header, return None if the type
    is a buildable FROM base, or a reason string explaining why not.

    Content-Type may include parameters (e.g. `application/...; charset=utf-8`);
    we look only at the bare mediaType prefix.
    """
    if not content_type:
        return None  # No type header → defer to build; can't classify
    ct = content_type.split(";", 1)[0].strip().lower()
    # Buildable types pass through.
    for ok in _BUILDABLE_MANIFEST_TYPES:
        if ct == ok.lower():
            return None
    # Known-bad types get a specific reason.
    for prefix, reason in _NON_IMAGE_MEDIATYPE_REASONS.items():
        if ct.startswith(prefix.lower()):
            return reason
    # Unknown type — flag it so the user knows the preflight isn't
    # sure but the build is likely to fail.
    return (
        f"The registry reports this artifact's mediaType as `{ct}`, "
        f"which isn't a Docker/OCI image manifest. The build will "
        f"likely fail at FROM resolution. If you're sure this is a "
        f"buildable image, ignore this warning and submit anyway — "
        f"but a typo or wrong-namespace ref is the more common cause."
    )


def _check_docker_hub(parsed: ParsedRef, ref: str, timeout: float) -> CheckResult:
    """Verify a Docker Hub ref actually resolves to a buildable
    container image (not a model artifact, attestation, or other
    OCI-shaped payload).

    Two-step check:

      1. Hit hub.docker.com/v2/repositories/<ns>/<repo>/ to confirm
         the repo exists. Cheap, public, no auth.
      2. Hit registry-1.docker.io/v2/<ns>/<repo>/manifests/<tag>
         to fetch the actual manifest's mediaType. Requires an
         anonymous pull token from auth.docker.io. The Content-Type
         response header reveals whether this is a container image
         vs an AI model vs something else.

    Stage repro 2026-05-21: `ai/stable-diffusion:Q4` passed the
    step-1 repo check (the repo exists on Docker Hub) but failed
    the build with `unknown type application/vnd.docker.ai.model.
    config.v0.1+json`. The step-2 manifest check catches that up
    front."""
    # Fast namespace shortcut for the `ai/` reserved namespace —
    # docker.io/ai/* is Docker's AI Model Registry. We don't even
    # need the manifest call to know it's not a container image.
    if parsed.namespace.lower() == "ai":
        return CheckResult(
            exists=False, ref=ref, parsed=parsed,
            registry_source="docker.io/ai (model registry)",
            reason=_NON_IMAGE_MEDIATYPE_REASONS[
                "application/vnd.docker.ai.model.config"
            ],
        )

    # Step 1: repo existence on hub.docker.com.
    url = f"https://hub.docker.com/v2/repositories/{parsed.namespace}/{parsed.repo}/"
    try:
        r = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("preflight: docker hub repo lookup failed: %s", exc)
        return CheckResult(
            exists=None, ref=ref, parsed=parsed, registry_source="docker.io",
            reason=f"Network error reaching Docker Hub: {exc}",
        )
    if r.status_code == 404:
        return CheckResult(
            exists=False, ref=ref, parsed=parsed, registry_source="docker.io",
            reason=(
                f"Docker Hub returned 404 for {parsed.namespace}/{parsed.repo}. "
                f"This namespace/repo combination doesn't exist as a public "
                f"Docker image. If you meant a HuggingFace model, those are "
                f"NOT Docker images — pick a container base like "
                f"huggingface/transformers-pytorch-gpu:latest and let the "
                f"build install diffusers / load the model at runtime."
            ),
        )
    if r.status_code != 200:
        return CheckResult(
            exists=None, ref=ref, parsed=parsed, registry_source="docker.io",
            reason=f"Docker Hub returned {r.status_code} — couldn't verify; build will retry.",
        )

    # Step 2: manifest mediaType check. This catches AI Model
    # artifacts, attestations, and other non-image OCI payloads
    # that step 1 can't distinguish from real images.
    token = _docker_hub_anon_token(parsed, timeout)
    if token is None:
        # Couldn't get a pull token. The repo exists per step 1; we
        # just can't verify the artifact type. Defer to build —
        # safer than blocking on a network blip.
        return CheckResult(
            exists=True, ref=ref, parsed=parsed,
            registry_source="docker.io",
            reason="repo exists; manifest type not verified (auth token unavailable)",
        )
    accept = ", ".join(_BUILDABLE_MANIFEST_TYPES)
    try:
        m = requests.head(
            f"https://registry-1.docker.io/v2/{parsed.namespace}/"
            f"{parsed.repo}/manifests/{parsed.tag}",
            headers={"Authorization": f"Bearer {token}", "Accept": accept},
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        logger.debug("preflight: manifest HEAD failed: %s", exc)
        return CheckResult(
            exists=True, ref=ref, parsed=parsed, registry_source="docker.io",
            reason=f"repo exists; manifest check failed ({exc}); deferring to build",
        )
    if m.status_code == 404:
        return CheckResult(
            exists=False, ref=ref, parsed=parsed, registry_source="docker.io",
            reason=(
                f"Docker Hub repo {parsed.namespace}/{parsed.repo} exists "
                f"but the tag `{parsed.tag}` doesn't. Pick a real tag — "
                f"check https://hub.docker.com/r/{parsed.namespace}/"
                f"{parsed.repo}/tags."
            ),
        )
    if m.status_code in (401, 403):
        # The repo exists but is private. We can't verify the
        # artifact type without creds. Defer.
        return CheckResult(
            exists=None, ref=ref, parsed=parsed, registry_source="docker.io",
            reason=(
                f"Docker Hub returned {m.status_code} for the manifest — "
                f"this repo is private. The build worker will need pull "
                f"creds; we can't verify the artifact type from here."
            ),
        )
    if m.status_code != 200:
        return CheckResult(
            exists=None, ref=ref, parsed=parsed, registry_source="docker.io",
            reason=f"Manifest HEAD returned {m.status_code} — couldn't verify; build will retry.",
        )

    # 200 from the manifest endpoint. Check the Content-Type.
    bad_reason = _classify_manifest_mediatype(m.headers.get("Content-Type", ""))
    if bad_reason is None:
        return CheckResult(
            exists=True, ref=ref, parsed=parsed, registry_source="docker.io",
        )
    return CheckResult(
        exists=False, ref=ref, parsed=parsed,
        registry_source="docker.io (manifest type check)",
        reason=bad_reason,
    )


def _check_anonymous_manifest(
    parsed: ParsedRef, ref: str, timeout: float, registry_label: str,
) -> CheckResult:
    """For registries that accept anonymous manifest requests (nvcr.io
    for public images, ghcr.io, etc.). Hits the v2 manifest endpoint
    directly."""
    url = (
        f"https://{parsed.registry_host}/v2/"
        f"{parsed.namespace}/{parsed.repo}/manifests/{parsed.tag}"
    )
    try:
        r = requests.get(
            url, timeout=timeout,
            headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
        )
        if r.status_code == 200:
            return CheckResult(
                exists=True, ref=ref, parsed=parsed,
                registry_source=registry_label,
            )
        if r.status_code == 404:
            return CheckResult(
                exists=False, ref=ref, parsed=parsed,
                registry_source=registry_label,
                reason=(
                    f"{registry_label} returned 404 for {parsed.namespace}/"
                    f"{parsed.repo}:{parsed.tag}."
                ),
            )
        if r.status_code in (401, 403):
            # Private image; we can't verify without creds. Defer.
            return CheckResult(
                exists=None, ref=ref, parsed=parsed,
                registry_source=registry_label,
                reason=(
                    f"{registry_label} returned {r.status_code} — likely a "
                    f"private image. The build worker will need pull creds; "
                    f"we can't verify existence from here."
                ),
            )
        return CheckResult(
            exists=None, ref=ref, parsed=parsed,
            registry_source=registry_label,
            reason=f"{registry_label} returned {r.status_code} — deferring to build.",
        )
    except requests.RequestException as exc:
        return CheckResult(
            exists=None, ref=ref, parsed=parsed,
            registry_source=registry_label,
            reason=f"Network error reaching {registry_label}: {exc}",
        )


def check_image_ref(ref: str, *, timeout: float = 5.0) -> CheckResult:
    """Verify a docker image reference resolves against its registry.

    Returns CheckResult with `exists` ∈ {True, False, None}:
      - True: registry confirmed the repo exists (tag not always checked).
      - False: registry returned 404 or similar definite "no".
      - None: couldn't verify (network error, unsupported registry,
              private-image 401/403). Caller MAY proceed with the build.
    """
    parsed = parse_image_ref(ref)
    if parsed is None:
        return CheckResult(
            exists=False, ref=ref, parsed=None,
            registry_source="syntax",
            reason="Reference is empty or malformed.",
        )

    # Common confusion: HuggingFace model paths aren't Docker images.
    host = parsed.registry_host.lower()
    if host in _NON_REGISTRY_HOSTS:
        return CheckResult(
            exists=False, ref=ref, parsed=parsed,
            registry_source="not-a-docker-registry",
            reason=(
                f"{host} is a model hub, not a Docker registry. To use a "
                f"HuggingFace model in a wizard build, pick a Docker base "
                f"image (e.g. huggingface/transformers-pytorch-gpu:latest) "
                f"and the entrypoint will load the model at runtime."
            ),
        )

    if host in ("docker.io", "registry-1.docker.io", "index.docker.io"):
        return _check_docker_hub(parsed, ref, timeout)
    if host == "nvcr.io":
        return _check_anonymous_manifest(parsed, ref, timeout, "nvcr.io")
    if host == "ghcr.io":
        return _check_anonymous_manifest(parsed, ref, timeout, "ghcr.io")
    if host == "quay.io":
        return _check_anonymous_manifest(parsed, ref, timeout, "quay.io")

    # Unknown registry — defer to build.
    return CheckResult(
        exists=None, ref=ref, parsed=parsed,
        registry_source=host,
        reason=(
            f"Registry {host} isn't on our preflight list — proceeding "
            f"to build. If the ref is wrong, the build will fail at FROM "
            f"resolution."
        ),
    )


# ---------------------------------------------------------------------------
# LLM-driven suggestions when the ref doesn't exist
# ---------------------------------------------------------------------------

_SUGGEST_PROMPT_TEMPLATE = """\
A user typed `{ref}` as a Docker image but it doesn't resolve against any public registry.

Their build context:
- Node display name: {node_name}
- Input type: {input_type}
- Output type: {output_type}
- Free-text description: {description}

Suggest 1-3 REAL, currently-published Docker images on Docker Hub or NGC \
that match the user's likely intent. Prefer images that:
- Are actively maintained (recently updated, official org accounts)
- Have CUDA support if input/output suggests GPU work (image, video, 3d, embedding)
- Are widely used so the build is likely to succeed

Return ONLY a JSON array. Each item: {{"ref": "namespace/repo:tag", "why": "1-sentence reason"}}.
No prose, no markdown, no code fences. Example shape:
[{{"ref": "huggingface/transformers-pytorch-gpu:latest", "why": "official HF image with PyTorch+CUDA"}}]
"""


def suggest_alternatives_llm(
    ref: str,
    node_name: str = "",
    input_type: str = "image",
    output_type: str = "image",
    description: str = "",
    llm_env: Optional[dict] = None,
    timeout: float = 15.0,
) -> List[dict]:
    """Call the platform LLM for replacement Docker image suggestions.
    Returns [] on any failure (network, parse, timeout) — callers
    should treat suggestions as advisory."""
    import json
    import os as _os

    # Resolve LLM env (defaults to whatever /generate-artifacts uses).
    if llm_env is None:
        try:
            from utils.tool_config import resolved_effective_env
            llm_env = resolved_effective_env("wizard")
        except Exception as exc:
            logger.warning("suggest_alternatives_llm: AI config lookup failed: %s", exc)
            return []

    base_url = (llm_env or {}).get("LLM_BASE_URL", "").rstrip("/")
    api_key = (llm_env or {}).get("LLM_API_KEY", "")
    model = (llm_env or {}).get("LLM_MODEL", "")
    if not base_url or not model:
        logger.info("suggest_alternatives_llm: no LLM configured, returning []")
        return []

    prompt = _SUGGEST_PROMPT_TEMPLATE.format(
        ref=ref,
        node_name=node_name or "(not set)",
        input_type=input_type,
        output_type=output_type,
        description=description or "(not set)",
    )

    try:
        r = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}" if api_key else "",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You return JSON only."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 400,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        body = r.json()
        content = body["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("suggest_alternatives_llm: LLM call failed: %s", exc)
        return []

    # Extract the JSON array. The LLM sometimes wraps in code fences
    # despite the instruction; strip them.
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?|\n?```$", "", content, flags=re.MULTILINE)

    try:
        suggestions = json.loads(content)
    except Exception as exc:
        logger.warning("suggest_alternatives_llm: JSON parse failed: %s", exc)
        return []

    if not isinstance(suggestions, list):
        return []

    # Validate shape. Each item must be {ref: str, why: str}.
    cleaned: List[dict] = []
    for s in suggestions[:3]:
        if not isinstance(s, dict):
            continue
        ref_val = (s.get("ref") or "").strip()
        why_val = (s.get("why") or "").strip()
        if not ref_val:
            continue
        cleaned.append({"ref": ref_val, "why": why_val})
    return cleaned


def manifest_list_architectures(manifest_list: dict) -> list:
    """Every linux architecture a manifest list offers, e.g. ["amd64","arm64"].

    Companion to _pick_inspectable_digest, which answers "where is a readable
    one" —
    a fair question when everything was amd64 and the only question. This
    answers "what can run this", which is what the scheduler needs before it
    hands an image to a worker whose CPU may not be able to execute it.

    Linux only, and `unknown` skipped: buildx attaches provenance and SBOM
    entries as `unknown/unknown` platform records, and counting those as
    architectures would advertise images that run nowhere.
    """
    out = []
    for entry in manifest_list.get("manifests", []) or []:
        if not isinstance(entry, dict):
            continue
        platform = entry.get("platform") or {}
        if platform.get("os") != "linux":
            continue
        arch = platform.get("architecture")
        if not isinstance(arch, str) or arch in ("", "unknown"):
            continue
        if arch not in out:
            out.append(arch)
    return out
