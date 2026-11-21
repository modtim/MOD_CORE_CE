"""Regression tests pinning recipe FROM-image refs to gpu_variants.VARIANT_BASES.

Past breakage: intel_xpu.py rendered Dockerfiles with
`FROM ghcr.io/modtim/base-intel:ipex2.6-python3.11`,
but VARIANT_BASES said the canonical Intel base was
`ghcr.io/modtim/base-intel-xpu:2025.0`. The recipe's
ref didn't exist in Harbor → every Intel build failed with `not found`.

These tests fail loudly if a recipe's _BASE_IMAGE diverges from the
canonical mapping, so the next reviewer notices the drift before
shipping a broken variant.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# tests/ lives at Containerizer/tests/; scripts/ is its sibling under
# Containerizer/. Resolve relative to this file so the test works
# regardless of where the Containerizer dir is mounted (e.g. /src in
# the docker test runner vs. the full repo path locally).
_CONTAINERIZER_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_DIR = _CONTAINERIZER_ROOT / "scripts" / "tools"

_GPU_VARIANTS_PY = _TOOLS_DIR / "gpu_variants.py"
_INTEL_XPU_PY = _TOOLS_DIR / "backends" / "recipes" / "intel_xpu.py"
_INTEL_NPU_PY = _TOOLS_DIR / "backends" / "recipes" / "intel_npu.py"


def _read_assignment(path: Path, name: str) -> str:
    # Source-grep for a top-level string assignment. Handles both
    # single-line "_BASE_IMAGE = '...'" and triple-quoted
    # _DOCKERFILE_TEMPLATE = (triple-quote) ... (triple-quote)
    # assignments. We pull values via regex rather than importing the
    # module because the recipe's package root depends on the rest of
    # the Containerizer tooling (gitingest, llm_providers, etc.) which
    # isn't available in a unit test runner.
    text = path.read_text()
    # Try triple-quoted first (greedy across lines).
    m = re.search(
        rf'^\s*{re.escape(name)}\s*=\s*"""(.*?)"""',
        text, re.DOTALL | re.MULTILINE,
    )
    if m:
        return m.group(1)
    # Fall back to single-line single/double quoted.
    m = re.search(rf'^\s*{re.escape(name)}\s*=\s*[\'"]([^\'"]+)[\'"]', text, re.MULTILINE)
    if not m:
        raise AssertionError(f"{name} string assignment not found in {path}")
    return m.group(1)


def _read_dict_entry(path: Path, dict_name: str, key: str) -> str:
    """Source-grep for `dict_name = { ... "key": "value", ... }` and
    return the value string. Same rationale as _read_assignment — we
    avoid importing the parent package."""
    text = path.read_text()
    # Find the dict block (greedy across lines until the matching `}`).
    block = re.search(
        rf'{re.escape(dict_name)}\s*:\s*Dict\[.+?\]\s*=\s*\{{(.+?)\n\}}',
        text,
        re.DOTALL,
    )
    if not block:
        # Fallback: untyped assignment.
        block = re.search(
            rf'{re.escape(dict_name)}\s*=\s*\{{(.+?)\n\}}',
            text,
            re.DOTALL,
        )
    if not block:
        raise AssertionError(f"{dict_name} dict not found in {path}")
    body = block.group(1)
    m = re.search(rf'[\'"]{re.escape(key)}[\'"]\s*:\s*[\'"]([^\'"]+)[\'"]', body)
    if not m:
        raise AssertionError(f"{dict_name}[{key!r}] entry not found in {path}")
    return m.group(1)


@pytest.fixture(scope="module")
def variant_bases_intel():
    return _read_dict_entry(_GPU_VARIANTS_PY, "VARIANT_BASES", "intel")


@pytest.fixture(scope="module")
def intel_xpu_base_image():
    return _read_assignment(_INTEL_XPU_PY, "_BASE_IMAGE")


@pytest.fixture(scope="module")
def intel_xpu_dockerfile_template():
    return _read_assignment(_INTEL_XPU_PY, "_DOCKERFILE_TEMPLATE")


class TestIntelXPURecipeBaseImage:

    def test_base_image_matches_variant_bases_intel_entry(
        self, intel_xpu_base_image, variant_bases_intel,
    ):
        """The recipe's _BASE_IMAGE must be exactly what
        VARIANT_BASES["intel"] says it is. If they diverge the
        rendered Dockerfile points at a non-existent image."""
        assert intel_xpu_base_image == variant_bases_intel, (
            f"intel_xpu._BASE_IMAGE ({intel_xpu_base_image!r}) "
            f"diverges from gpu_variants.VARIANT_BASES['intel'] "
            f"({variant_bases_intel!r}). One of them is wrong; both "
            f"must agree."
        )

    def test_dockerfile_template_pulls_base_image_from_constant(
        self, intel_xpu_dockerfile_template,
    ):
        # The template's FROM line MUST use the {base_image} placeholder
        # so rendering goes through _BASE_IMAGE. A literal hard-coded ref
        # in the template would let the recipe drift from VARIANT_BASES
        # through a parallel code path.
        assert "FROM {base_image}" in intel_xpu_dockerfile_template, (
            "template must reference {base_image}; got first 200 chars: "
            + intel_xpu_dockerfile_template[:200]
        )
        # And it must be the ONLY FROM line — guards against someone
        # adding a multi-stage build with a hardcoded second FROM.
        from_lines = [l for l in intel_xpu_dockerfile_template.splitlines()
                      if l.strip().startswith("FROM ")]
        assert len(from_lines) == 1, (
            f"expected exactly one FROM line; got {from_lines!r}"
        )

    def test_base_image_uses_full_harbor_prefix(self, intel_xpu_base_image):
        """A bare `core/base-intel:...` (without the registry.example.com
        prefix) is the failure mode the user hit. Hard-pin the prefix
        so the recipe never regresses to a registry-less ref."""
        assert intel_xpu_base_image.startswith(
            "ghcr.io/modtim/"
        ), (
            f"_BASE_IMAGE must include the full registry.example.com/"
            f"modtim/ prefix; got {intel_xpu_base_image!r}"
        )

    def test_base_image_is_not_the_old_broken_ref(self, intel_xpu_base_image):
        """Explicit guard against the exact ref that broke: if anyone
        copy-pastes the old comment back in, this fails immediately
        with a pointer to the bug."""
        assert "base-intel:ipex2.6-python3.11" not in intel_xpu_base_image, (
            "intel_xpu._BASE_IMAGE regressed to the broken "
            "`base-intel:ipex2.6-python3.11` ref that doesn't exist "
            "in Harbor. Use base-intel-xpu:2025.0."
        )


@pytest.fixture(scope="module")
def variant_bases_npu():
    return _read_dict_entry(_GPU_VARIANTS_PY, "VARIANT_BASES", "npu")


@pytest.fixture(scope="module")
def intel_npu_base_image():
    return _read_assignment(_INTEL_NPU_PY, "_BASE_IMAGE")


@pytest.fixture(scope="module")
def intel_npu_dockerfile_template():
    return _read_assignment(_INTEL_NPU_PY, "_DOCKERFILE_TEMPLATE")


class TestIntelNPURecipeBaseImage:
    """Mirror of TestIntelXPURecipeBaseImage for the intel_npu recipe.

    Past breakage: intel_npu.py rendered Dockerfiles with
    `FROM ghcr.io/modtim/base-openvino:2025.0-python3.11`,
    a repo+tag combination that doesn't exist in Harbor (the curated
    NPU base is `base-npu:openvino-2025`). The intel_xpu recipe had
    parallel coverage that caught its drift; intel_npu didn't, so the
    same class of bug shipped.
    """

    def test_base_image_matches_variant_bases_npu_entry(
        self, intel_npu_base_image, variant_bases_npu,
    ):
        assert intel_npu_base_image == variant_bases_npu, (
            f"intel_npu._BASE_IMAGE ({intel_npu_base_image!r}) "
            f"diverges from gpu_variants.VARIANT_BASES['npu'] "
            f"({variant_bases_npu!r}). One of them is wrong; both "
            f"must agree."
        )

    def test_dockerfile_template_pulls_base_image_from_constant(
        self, intel_npu_dockerfile_template,
    ):
        assert "FROM {base_image}" in intel_npu_dockerfile_template, (
            "template must reference {base_image}; got first 200 chars: "
            + intel_npu_dockerfile_template[:200]
        )
        from_lines = [l for l in intel_npu_dockerfile_template.splitlines()
                      if l.strip().startswith("FROM ")]
        assert len(from_lines) == 1, (
            f"expected exactly one FROM line; got {from_lines!r}"
        )

    def test_base_image_uses_full_harbor_prefix(self, intel_npu_base_image):
        assert intel_npu_base_image.startswith(
            "ghcr.io/modtim/"
        ), (
            f"_BASE_IMAGE must include the full registry.example.com/"
            f"modtim/ prefix; got {intel_npu_base_image!r}"
        )

    def test_base_image_is_not_the_old_broken_ref(self, intel_npu_base_image):
        """Explicit guard against the exact broken ref. Same pattern as
        the intel_xpu test — if someone copy-pastes the old assignment
        back, this fails immediately with a pointer to the bug."""
        assert "base-openvino:2025.0-python3.11" not in intel_npu_base_image, (
            "intel_npu._BASE_IMAGE regressed to the broken "
            "`base-openvino:2025.0-python3.11` ref that doesn't exist "
            "in Harbor. Use base-npu:openvino-2025."
        )
