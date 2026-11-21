"""Curated catalog of base images KNOWN to exist on docker.io /
nvcr.io / our own harbor. Used by:
  - critic_repair_fix.py — deterministic pre-build check that catches
    hallucinated tags before docker build burns 5-15 min on them.
  - failure_category.py — IMAGE_NOT_FOUND hint lists these so the
    repair LLM has a known-good set to pick from.

Curation policy: every entry must have been manifest-resolved at
least once. New entries get added when a successful build uses them
(or when an operator vets them). NEVER add a tag without confirming
it resolves — the whole point of this file is to make hallucination
detection deterministic.

Generated 2026-05-27 from the bases that successfully built in
wfr 46/48 (nerfstudio) and the prior wizard test runs. Verified
against `docker manifest inspect`.
"""
from __future__ import annotations

import re
from typing import Optional
from utils.registries import platform_image


# Each entry is a regex matching the FULL image reference (no tag
# wildcard — exact tags only). Tags listed here MUST resolve.
_KNOWN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # NVIDIA CUDA — docker.io/nvidia/cuda — the runtime / devel / cudnn variants
    # that successfully built in wfr 48.
    re.compile(r"^nvidia/cuda:12\.4\.1-(runtime|devel|cudnn-runtime|cudnn-devel)-ubuntu22\.04$"),
    re.compile(r"^nvidia/cuda:12\.4\.0-(runtime|devel|cudnn-runtime|cudnn-devel)-ubuntu22\.04$"),
    re.compile(r"^nvidia/cuda:12\.1\.1-(runtime|devel|cudnn8-runtime|cudnn8-devel)-ubuntu22\.04$"),
    re.compile(r"^nvidia/cuda:11\.8\.0-(runtime|devel|cudnn8-runtime|cudnn8-devel)-ubuntu22\.04$"),
    # NVIDIA NGC (nvcr.io). Catalog at https://catalog.ngc.nvidia.com
    re.compile(r"^nvcr\.io/nvidia/pytorch:(24|23)\.\d{2}-py3$"),
    re.compile(r"^nvcr\.io/nvidia/cuda:12\.4\.1-(runtime|devel|cudnn-runtime|cudnn-devel)-ubuntu22\.04$"),
    # PyTorch official — confirmed resolvable.
    re.compile(r"^pytorch/pytorch:2\.4\.0-cuda12\.1-cudnn9-(runtime|devel)$"),
    re.compile(r"^pytorch/pytorch:2\.3\.1-cuda12\.1-cudnn8-(runtime|devel)$"),
    re.compile(r"^pytorch/pytorch:2\.1\.0-cuda11\.8-cudnn8-(runtime|devel)$"),
    # ROCm — official AMD images.
    re.compile(r"^rocm/dev-ubuntu-22\.04:6\.\d+(\.\d+)?$"),
    re.compile(r"^rocm/pytorch:rocm6\.\d+(\.\d+)?_(ubuntu22\.04)_py3\.\d+_pytorch_\d+\.\d+(\.\d+)?$"),
    # Intel — oneAPI base.
    re.compile(r"^intel/oneapi-basekit:\d{4}\.\d+(\.\d+)?-devel-ubuntu22\.04$"),
    # Generic Python / Node / Ubuntu (always-existing).
    re.compile(r"^python:3\.(11|12|13)(-slim|-slim-bookworm)?$"),
    re.compile(r"^node:(20|22)(-slim|-alpine)?$"),
    re.compile(r"^ubuntu:(22\.04|24\.04)$"),
    re.compile(r"^debian:(bookworm-slim|bullseye-slim)$"),
    re.compile(r"^alpine:3\.(18|19|20)$"),
    # Our own harbor — only the variants confirmed published.
    # `cuda-12.4-python3.11-slim` is NOT here because it doesn't exist.
    re.compile(r"^ghcr\.io/modtim/base-cuda:12\.4-python3\.11$"),
    re.compile(r"^ghcr\.io/modtim/base-rocm:6\.1-python3\.11$"),
    re.compile(r"^ghcr\.io/modtim/python-3\.11$"),
)


# Tags the repair LLM has hallucinated in past runs. These are
# EXPLICITLY not real — adding them to a denylist so the
# image-existence check can give a high-confidence "this doesn't
# exist" reject instead of just falling through to "not on allowlist".
_KNOWN_HALLUCINATED: frozenset[str] = frozenset({
    "nvidia/cuda:12.4.1-base",
    "nvidia/cuda:12.4.0-base",
    "nvidia/cuda:12.4-base",
    "nvidia/cuda:12.4.1-base-ubuntu22.04",
    platform_image("base-cuda:12.4-python3.11-slim"),
    platform_image("nerfstudio-cuda:12.4-python3.11"),
    platform_image("base-pytorch:2.4-cuda12.4"),
    # wfr 49 hallucinations — LLM kept inventing "PyTorch-with-CUDA"
    # NGC tags that don't follow NGC's actual YY.MM-py3 naming.
    "nvcr.io/nvidia/pytorch:2.3.0-py3.10-cuda12.1.0-devel-ubuntu22.04",
    "nvcr.io/nvidia/pytorch:2.4.0-py3.10-cuda12.1.0-devel-ubuntu22.04",
    "nvcr.io/nvidia/pytorch:23.10-py3.10-cuda12.1.0-devel-ubuntu22.04",
})

# Regex patterns the LLM has shown a tendency to invent. ANY match
# against these patterns is rejected without consulting the
# allowlist (because the structure itself is non-existent — NGC
# uses YY.MM, not version-with-cuda-with-os).
_HALLUCINATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # NGC pytorch in the wrong format. Real NGC tags are YY.MM-py3
    # (24.03-py3, 23.10-py3). The LLM keeps inventing
    # "<torch-ver>-py3.X-cuda<ver>-devel-ubuntu*" which is a
    # mashup of PyTorch official + nvidia/cuda naming.
    re.compile(r"^nvcr\.io/nvidia/pytorch:\d+\.\d+\.\d+-py3"),
    # nvidia/cuda :*-base (without -runtime/-devel/-cudnn). NVIDIA
    # publishes runtime, devel, cudnn variants — not bare :*-base.
    re.compile(r"^nvidia/cuda:[\d.]+-base($|-)"),
    # ghcr.io/modtim/*-slim variants — our harbor
    # doesn't ship any -slim suffix.
    re.compile(r"^ghcr\.io/modtim/[^:]+:[^-]+-slim"),
    # MOD harbor with a library name in the path — `base-cuda` and
    # `base-rocm` are the only library-named bases; anything else is
    # invented.
    re.compile(r"^ghcr\.io/modtim/(?!base-cuda|base-rocm|base-intel-xpu|base-npu|base-python|base-node|base-go|base-rust|base-java|base-alpine|base-distroless|base-pytorch|build-task|containerizer)"),
)


_FROM_LINE = re.compile(
    r"^\s*FROM\s+([A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?)",
    re.MULTILINE | re.IGNORECASE,
)


def extract_from_images(dockerfile: str) -> list[str]:
    """Return all FROM image references from a Dockerfile. Multi-
    stage builds produce multiple FROMs; each is checked individually.
    The 'AS name' suffix is stripped — we only care about the image
    ref, not the stage alias."""
    if not dockerfile:
        return []
    refs: list[str] = []
    for m in _FROM_LINE.finditer(dockerfile):
        ref = m.group(1).strip()
        if ref:
            refs.append(ref)
    return refs


def is_known_existing(image_ref: str) -> bool:
    """True when ``image_ref`` matches our curated allowlist of
    bases known to resolve. False does NOT mean "doesn't exist" —
    it just means we don't have an allowlist entry. Caller decides
    whether to reject or fall through."""
    if not image_ref:
        return False
    image_ref = image_ref.strip()
    return any(p.match(image_ref) for p in _KNOWN_PATTERNS)


def is_known_hallucinated(image_ref: str) -> bool:
    """True when ``image_ref`` matches the explicit denylist OR a
    known-hallucination structural pattern. The patterns catch
    "shape-of-hallucination" cases the explicit list can't enumerate
    (every new wrong tag the LLM invents would otherwise slip past
    until it gets added to the list)."""
    ref = (image_ref or "").strip()
    if ref in _KNOWN_HALLUCINATED:
        return True
    for pat in _HALLUCINATION_PATTERNS:
        if pat.match(ref):
            return True
    return False


def validate_dockerfile_from_lines(dockerfile: str) -> Optional[str]:
    """Inspect all FROM lines in a Dockerfile. Returns a rejection
    reason string when ANY FROM image is on the hallucination
    denylist. Returns None when all FROMs are either on the
    allowlist or unknown (we don't reject unknowns — that's the
    repair LLM's call).

    Use case: critic_repair_fix.py wraps the repair LLM's proposed
    Dockerfile and rejects it here BEFORE the build farm wastes
    5-15 min on a doomed docker build.
    """
    refs = extract_from_images(dockerfile)
    bad: list[str] = []
    for ref in refs:
        if is_known_hallucinated(ref):
            bad.append(ref)
    if not bad:
        return None
    joined = ", ".join(bad)
    return (
        f"Dockerfile FROM references known-hallucinated tags: {joined}. "
        f"These tags do NOT exist on docker.io / nvcr.io / our harbor. "
        f"Pick from the curated KNOWN-EXISTING list in the failure-category "
        f"hint (nvidia/cuda:12.4.1-{{runtime,devel,cudnn-runtime,cudnn-devel}}-ubuntu22.04, "
        f"pytorch/pytorch:2.4.0-cuda12.1-cudnn9-{{runtime,devel}}, etc.). "
        f"Do not invent new tags."
    )
