"""Image-mode adapter (wizard-mode-rebuild §2).

When a user submits the wizard in Image mode — they supplied a docker
ref like `runwayml/stable-diffusion-v1-5` and nothing else — the
Containerizer LLM should NOT be dispatched to write a Dockerfile
from scratch. The user already chose their base. This module produces
the thin adapter directly:

  FROM <user-ref>
  USER root
  RUN mkdir -p /data/input /data/output && chown -R mod:mod /data
  COPY entrypoint.sh /entrypoint.sh
  RUN chmod +x /entrypoint.sh
  USER mod
  ENTRYPOINT ["/entrypoint.sh"]

The entrypoint.sh is template-based, picked by `(input_type,
output_type)` pair. Templates live in `entrypoint_templates/`. v1
ships with text→image for the diffusion case Tim's chasing; more
pairs land as needed. Unknown pairs fall through to a generic
"exec the image's existing CMD with /data/input/ mounted" template
that works for any image whose native CMD already handles input/output
via env vars or arguments.

If the template doesn't fit a user's image, the build's test-and-
repair loop can iterate. Worst case: the user re-runs the wizard in
Script mode and writes the invocation themselves.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


_TEMPLATE_DIR = Path(__file__).parent / "entrypoint_templates"


def _slug_for_pair(input_type: str, output_type: str) -> str:
    """Templates live at `<input_type>_to_<output_type>.sh` so the
    filename is grep-able and self-documenting."""
    return f"{(input_type or 'any').lower()}_to_{(output_type or 'any').lower()}.sh"


def _load_template(input_type: str, output_type: str) -> str:
    """Look up the (input_type, output_type) template; fall back to the
    generic 'any_to_any.sh' if no specific pair matches."""
    specific = _TEMPLATE_DIR / _slug_for_pair(input_type, output_type)
    if specific.exists():
        return specific.read_text()
    generic = _TEMPLATE_DIR / "any_to_any.sh"
    if generic.exists():
        return generic.read_text()
    # Defensive fallback — should never hit since any_to_any.sh ships.
    logger.warning(
        "io_adapter: no template found for (%s, %s) — emitting minimal stub",
        input_type, output_type,
    )
    return (
        "#!/bin/bash\nset -e\n"
        "echo 'ERROR: no entrypoint template — re-run wizard in Script mode' >&2\n"
        "exit 1\n"
    )


def build_image_mode_dockerfile(image_ref: str) -> str:
    """Produce the thin adapter Dockerfile for an Image-mode submission.

    Starts with `FROM <image_ref>` exactly — no curated-base
    substitution, no LLM-generated boilerplate. The /data/input and
    /data/output directories are created world-writable so the
    container's existing runtime user (whatever the base image
    declares — root, mod, www-data, nobody, etc.) can read/write.

    Design decision: we deliberately DO NOT inject `useradd mod` or
    `USER mod`. Earlier versions did, and that approach broke on
    images that:
      - lack the `useradd` binary (distroless, scratch-based, etc.)
      - already provision a `mod` user with a fixed UID
      - have an existing ENTRYPOINT that assumes its own user
    The wizard-corruption detector even flags `useradd mod` as a
    known-bad LLM repair pattern (stage repro 2026-05-21,
    ai/stable-diffusion:Q4 build), so emitting it in our adapter
    template was guaranteed to trip that gate.

    Trade-off: when the base image runs as root, our adapter inherits
    that. The Dockerfile-lint `USER-root-final` rule applies to
    EXPLICIT `USER root` directives, not inherited base-image users,
    so this is fine for the lint pass. Operators who want
    non-root-runtime should pick a base image whose default user is
    non-root (or use Script mode for full control).
    """
    return (
        f"FROM {image_ref}\n"
        f"RUN mkdir -p /data/input /data/output && "
        f"chmod 1777 /data/input /data/output\n"
        f"COPY entrypoint.sh /entrypoint.sh\n"
        f"RUN chmod +x /entrypoint.sh\n"
        f"ENTRYPOINT [\"/entrypoint.sh\"]\n"
    )


def build_image_mode_entrypoint(
    input_type: str,
    output_type: str,
    image_ref: str,
) -> str:
    """Pick the entrypoint template for the (input_type, output_type)
    pair. The template is a complete bash script — no LLM gap-fill.

    Templates use simple substitution markers:
      {{ image_ref }}   → the user's docker ref (rarely needed in the
                          script body; available for diagnostics).
    """
    body = _load_template(input_type, output_type)
    return body.replace("{{ image_ref }}", image_ref)


def build_image_mode_build_and_push_sh(image_name: str) -> str:
    """The build-and-push.sh script is dead-simple for Image mode —
    we already know the image_name. The build-task containers source
    it for variable definitions and then run their own docker build
    + push commands."""
    return (
        f"#!/bin/sh\n"
        f"# Image mode — thin adapter wrapping a user-supplied base.\n"
        f"# This script defines the image_name + tag pattern the\n"
        f"# build-task uses; the actual docker build/push runs in\n"
        f"# the build-task container, not here.\n"
        f"export IMAGE_NAME={image_name!r}\n"
        f"export ADAPTER_MODE=image\n"
    )


def build_image_mode_artifacts(
    image_ref: str,
    input_type: str,
    output_type: str,
    image_name: str,
) -> Dict[str, str]:
    """Synthesize the three Albedo-pattern artifacts for an Image-mode
    submission. No LLM call. Returned dict has the same keys the
    Containerizer would have produced, so the ingest-artifacts merge
    path stays unchanged.

    Returns:
        dict with keys: dockerfile, entrypoint_sh, build_and_push_sh.
        Caller assigns these to BuildSpec.hero_dockerfile,
        BuildSpec.entrypoint_sh, BuildSpec.build_and_push_sh.
    """
    return {
        "dockerfile": build_image_mode_dockerfile(image_ref),
        "entrypoint_sh": build_image_mode_entrypoint(input_type, output_type, image_ref),
        "build_and_push_sh": build_image_mode_build_and_push_sh(image_name),
    }


def is_image_mode_submission(request: dict) -> bool:
    """Return True iff the request is a pure Image-mode submission:
    user supplied a wrap_base_image and nothing else that would
    require LLM-driven Dockerfile generation.

    Image mode is defined as:
      - wrap_base_image is set (non-empty)
      - inline_source_body is absent or empty (no pasted script)
      - source_repo_url is absent or empty (not a git submission)
      - additional_instructions is absent or empty (no LLM extras
        that imply the user wants a wrapper)

    If the user supplies additional_instructions alongside
    wrap_base_image, we route to the Containerizer because they're
    asking the LLM to do more than just register the image.
    """
    if not (request.get("wrap_base_image") or "").strip():
        return False
    if (request.get("inline_source_body") or "").strip():
        return False
    if (request.get("source_repo_url") or "").strip():
        return False
    if (request.get("additional_instructions") or "").strip():
        return False
    return True
