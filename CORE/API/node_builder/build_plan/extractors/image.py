"""Image-mode BuildPlan extractor (wizard-rebuild §3d).

Produces a `BuildPlan` from a wizard request that specifies
`wrap_base_image` and nothing else. No LLM call — the plan is
populated directly from the user's request + a digest lookup for
reproducibility.

Replaces (incrementally) the existing
`node_builder/io_adapter/image_mode.py` flow. For this chunk both
paths coexist: the new extractor produces a BuildPlan that, when
rendered, yields byte-identical artifacts to what io_adapter emits
today. Subsequent chunks delete io_adapter once Git/Dockerfile/
Script mode extractors land and the endpoint fully switches over.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from utils.image_ref_preflight import resolve_runtime_user

from ..types import (
    EXTRACTOR_VERSION,
    BuildPlan,
    IOPort,
    default_ports_for_types,
)

logger = logging.getLogger(__name__)


def extract_image_plan(
    *,
    image_ref: str,
    node_name: str,
    input_type: str = "image",
    output_type: str = "image",
    image_name: Optional[str] = None,
    container_env: Optional[dict[str, str]] = None,
    pin_digest: bool = True,
) -> BuildPlan:
    """Build a `BuildPlan` for an Image-mode submission.

    Args:
      image_ref: the docker ref the user typed (e.g.
        `runwayml/stable-diffusion-v1-5`).
      node_name: human-readable Node display name.
      input_type / output_type: legacy singleton port types.
        Multi-port plans set `input_ports` directly via a higher-
        level API; this helper bridges legacy-shape requests.
      image_name: optional override (the wizard normally derives
        this from node_name; we accept either).
      container_env: env vars from the wizard's Configure step.
      pin_digest: if True (default), resolve the ref's sha256
        digest at extract time so rebuilds are reproducible
        (wizard-rebuild §3d "version pinning"). Skipped silently
        on network failure.

    Returns:
      A `BuildPlan` whose `mode="image"`, `from_image_ref=image_ref`,
      `from_digest=<sha256:...>` (if pin succeeded), and singleton
      I/O ports matching the legacy types.

    No LLM call. No network call except the optional digest pin.
    Image-mode is the simplest extractor — the whole point of §2
    was that we don't need an LLM here.
    """
    in_ports, out_ports = default_ports_for_types(input_type, output_type)

    # Convert container_env dict to EnvVar list.
    env_vars = []
    if container_env:
        from ..types import EnvVar
        for name, default in container_env.items():
            if name:
                env_vars.append(EnvVar(
                    name=name,
                    required=True,
                    default=str(default) if default is not None else None,
                ))

    # Pin to digest for reproducibility. Best-effort; if the registry
    # is unreachable or rate-limits us, we keep the plain ref. The
    # build's pull will still resolve to whatever the registry has
    # at build-time — caller can re-run extraction later to pin.
    from_digest: Optional[str] = None
    if pin_digest:
        try:
            from_digest = _resolve_digest(image_ref)
        except Exception as exc:
            logger.warning(
                "extract_image_plan: digest pin failed for %r: %s "
                "(continuing without pin)", image_ref, exc,
            )

    # Detect the base image's declared runtime user so the rendered
    # adapter can `USER root` for the mkdir+chmod work and restore
    # the original at the end. Falls back to "" (= run as root) on
    # any lookup failure — same Dockerfile shape as before the fix.
    runtime_user: Optional[str] = None
    try:
        runtime_user = resolve_runtime_user(image_ref)
    except Exception as exc:
        logger.warning(
            "extract_image_plan: runtime_user lookup failed for %r: %s "
            "(continuing as root-runtime)", image_ref, exc,
        )

    plan = BuildPlan(
        mode="image",
        language="container",  # Image mode doesn't know the base's lang
        package_manager="none",  # Nothing to install — base has it baked
        workdir="/data/input",  # Convention for Image-mode adapters
        entrypoint=["/entrypoint.sh"],

        from_image_ref=image_ref,
        from_digest=from_digest,
        runtime_user=runtime_user,

        input_ports=in_ports,
        output_ports=out_ports,
        env_vars=env_vars,

        # Image-mode plans are HIGH confidence by definition — the
        # user explicitly chose their base; we're just wrapping it.
        # The only weakness is the entrypoint template selection,
        # which we cover via the (input_type, output_type) pair.
        confidence=0.95,
        extractor_version=EXTRACTOR_VERSION,
    )
    return plan


def _resolve_digest(image_ref: str) -> Optional[str]:
    """Resolve a docker ref to its current sha256 digest.

    Uses the existing `utils.image_ref_preflight` machinery (§2.5)
    which already has the registry-API token + manifest-fetch logic
    plumbed. We reach into the parsed-ref + auth-token helpers
    rather than rolling our own.

    Returns:
      "sha256:abc123..." on success, or None if we couldn't resolve.
    """
    from utils.image_ref_preflight import (
        _docker_hub_anon_token,
        parse_image_ref,
    )
    import requests

    parsed = parse_image_ref(image_ref)
    if parsed is None:
        return None

    # Public Docker Hub: token + manifest fetch path. The
    # preflight module already verified the ref exists; here we
    # just want the digest from the manifest response.
    if parsed.registry_host in ("docker.io", "registry-1.docker.io", "index.docker.io"):
        token = _docker_hub_anon_token(parsed, timeout=5.0)
        if not token:
            return None
        try:
            r = requests.head(
                f"https://registry-1.docker.io/v2/"
                f"{parsed.namespace}/{parsed.repo}/manifests/{parsed.tag}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": (
                        "application/vnd.docker.distribution.manifest.v2+json, "
                        "application/vnd.oci.image.manifest.v1+json, "
                        "application/vnd.oci.image.index.v1+json"
                    ),
                },
                timeout=5.0,
                allow_redirects=True,
            )
        except requests.RequestException:
            return None
        if r.status_code == 200:
            # Docker registries return the digest in the
            # `Docker-Content-Digest` header.
            digest = r.headers.get("Docker-Content-Digest")
            if digest and digest.startswith("sha256:"):
                return digest

    # Other registries (nvcr.io, ghcr.io, etc.) — same shape but
    # without the auth-token dance. Not implemented yet; falls back
    # to None which is fine.
    return None
