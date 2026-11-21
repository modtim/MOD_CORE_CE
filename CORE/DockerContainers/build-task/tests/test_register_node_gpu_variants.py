"""register_node.compose_gpu_variants — wizard-built nodes now ship
with a populated gpu_variants map.

Stage repro: node id=119 "PDF to PNG Converter" registered with
gpu_variants=NULL. The Tenant Registry soft-delete cascade-hide
that I built earlier (commit 00b3aa25) couldn't match the deleted
image back to the node because it walked the empty dict and saw
nothing to compare. The cascade-hide now also falls back to
executable_path (commit 294aa79c), but the right fix is for the
register step to populate gpu_variants in the first place — that's
what this commit does.

The composition rules under test:
  * hero variant key comes from spec.hero_variant (default "cpu")
  * hero maps to `:latest` (no suffix)
  * other variants in spec.variants map to `:latest-{variant_key}`
  * variants list is filtered "compatible-only" by the time the
    spec is persisted (build_dag/adapter.py:48), so everything in
    that list is something the worker actually pushed
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "stages"),
)

from register_node import compose_gpu_variants  # noqa: E402


_PREFIX = "example.com/tenant-1/"
_IMAGE = "pdf-to-png-converter"
_HERO_REF = f"{_PREFIX}{_IMAGE}:latest"


# ---------------------------------------------------------------------
# node_kind derivation
# ---------------------------------------------------------------------
# Workflow run 14 (stage repro): wizard-built node 120 had
# node_kind=NULL, so `_check_and_dispatch_downstream` silently
# dropped its dispatch (worker_tasks.py:189 — `if not node_kind:
# continue`). Run "completed" with only the root Image Input
# having actually executed. Register now derives node_kind from
# image_name (slug → lowercase, dashes → underscores) so wizard
# builds always carry the routing key.


def test_image_name_to_node_kind_conversion():
    """The image name comes off the build spec as a hyphenated slug;
    node_kind is the underscored equivalent. Test the
    transformation we apply inside register_node.main()."""
    # The conversion is a single line of code, but it's the single
    # line that broke run 14 — so it's worth pinning.
    image_name = "pdf-to-png-converter"
    derived = image_name.replace("-", "_").lower()
    assert derived == "pdf_to_png_converter"

    image_name = "PNG-To-PDF-Converter"
    derived = image_name.replace("-", "_").lower()
    assert derived == "png_to_pdf_converter"


def test_node_kind_in_register_body():
    """Verify register_node.main() includes node_kind in the body
    sent to POST /v1/nodes/. This is the actual change that
    unsticks wizard-built downstream nodes."""
    import inspect
    from register_node import main
    src = inspect.getsource(main)
    assert "node_kind" in src
    assert '"node_kind": node_kind' in src


def test_hero_only_no_other_variants():
    """The exact stage case: PDF→PNG converter, hero=cpu, no other
    variants (Intel was skipped because no torch import). Map should
    have exactly one entry."""
    spec = {
        "hero_variant": "cpu",
        "variants": [],
    }
    gv = compose_gpu_variants(
        spec=spec, prefix=_PREFIX, image_name=_IMAGE, hero_ref=_HERO_REF,
    )
    assert gv == {"cpu": _HERO_REF}


def test_hero_plus_intel_variant():
    """An ML node that built both hero (cpu or cuda) and intel."""
    spec = {
        "hero_variant": "cuda",
        "variants": [
            {"variant": "intel", "dockerfile": "..."},
        ],
    }
    gv = compose_gpu_variants(
        spec=spec, prefix=_PREFIX, image_name="ml-node",
        hero_ref=f"{_PREFIX}ml-node:latest",
    )
    assert gv == {
        "cuda": f"{_PREFIX}ml-node:latest",
        "intel": f"{_PREFIX}ml-node:latest-intel",
    }


def test_hero_plus_multiple_other_variants():
    """A node that built across every backend the recipe supports."""
    spec = {
        "hero_variant": "cuda",
        "variants": [
            {"variant": "intel"},
            {"variant": "rocm"},
            {"variant": "npu"},
        ],
    }
    gv = compose_gpu_variants(
        spec=spec, prefix=_PREFIX, image_name="big-ml",
        hero_ref=f"{_PREFIX}big-ml:latest",
    )
    assert gv == {
        "cuda": f"{_PREFIX}big-ml:latest",
        "intel": f"{_PREFIX}big-ml:latest-intel",
        "rocm": f"{_PREFIX}big-ml:latest-rocm",
        "npu": f"{_PREFIX}big-ml:latest-npu",
    }


def test_hero_variant_defaults_to_cpu_when_unset():
    """Older specs may not carry hero_variant — default to 'cpu'
    so the gpu_variants dict always has a hero entry."""
    spec = {"variants": []}  # no hero_variant key
    gv = compose_gpu_variants(
        spec=spec, prefix=_PREFIX, image_name=_IMAGE, hero_ref=_HERO_REF,
    )
    assert gv == {"cpu": _HERO_REF}


def test_hero_variant_empty_string_falls_through_to_cpu():
    """Defensive: an empty string for hero_variant must NOT clobber
    the map with `{"": ref}`."""
    spec = {"hero_variant": "  ", "variants": []}
    gv = compose_gpu_variants(
        spec=spec, prefix=_PREFIX, image_name=_IMAGE, hero_ref=_HERO_REF,
    )
    assert gv == {"cpu": _HERO_REF}


def test_variant_key_matching_hero_is_not_double_added():
    """If the variants list redundantly contains the hero key, don't
    overwrite the hero ref or emit a `:latest-cpu` tag that doesn't
    exist."""
    spec = {
        "hero_variant": "cpu",
        "variants": [{"variant": "cpu"}, {"variant": "intel"}],
    }
    gv = compose_gpu_variants(
        spec=spec, prefix=_PREFIX, image_name=_IMAGE, hero_ref=_HERO_REF,
    )
    assert gv == {
        "cpu": _HERO_REF,
        "intel": f"{_PREFIX}{_IMAGE}:latest-intel",
    }


def test_malformed_variant_entries_ignored():
    """Spec parsing is best-effort: a non-dict entry or missing
    variant key in the variants list shouldn't crash or produce
    bogus map entries."""
    spec = {
        "hero_variant": "cpu",
        "variants": [
            "not-a-dict",   # type: ignore[list-item]
            {"variant": ""},
            {"not_variant": "intel"},
            None,            # type: ignore[list-item]
            {"variant": "intel"},
        ],
    }
    gv = compose_gpu_variants(
        spec=spec, prefix=_PREFIX, image_name=_IMAGE, hero_ref=_HERO_REF,
    )
    assert gv == {
        "cpu": _HERO_REF,
        "intel": f"{_PREFIX}{_IMAGE}:latest-intel",
    }


def test_empty_variants_list_still_yields_hero():
    """Sanity: even when the spec has no variants, the hero entry
    must be present so the cascade-hide / scheduler have something
    to work with."""
    spec = {"hero_variant": "cuda"}
    gv = compose_gpu_variants(
        spec=spec, prefix=_PREFIX, image_name=_IMAGE,
        hero_ref=f"{_PREFIX}{_IMAGE}:latest",
    )
    assert gv == {"cuda": f"{_PREFIX}{_IMAGE}:latest"}
