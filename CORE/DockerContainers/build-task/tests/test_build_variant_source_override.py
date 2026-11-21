"""Unit tests for operator per-variant source overrides in build_one_variant.

The wizard Preview "Rebuild {variant} with my changes" flow records the
operator's edits on the BuildSpec as `source_overrides_by_variant[variant]`.
build_one_variant must stage those edits OVER the base source_files for that
one variant, while leaving every normal build (no overrides) byte-identical.

Covers the pure `_resolve_staged_sources` helper (no docker, no filesystem).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stages"))

import build_one_variant as bov  # noqa: E402


BASE = {
    "image_name": "img",
    "source_files": [
        {"filename": "main.py", "content": "print('hero')"},
        {"filename": "util.py", "content": "X = 1"},
    ],
}


def test_no_overrides_is_byte_identical_to_base():
    # Every normal build path: no override channel → staged == source_files.
    staged = bov._resolve_staged_sources(BASE, "cuda")
    assert staged == {"main.py": "print('hero')", "util.py": "X = 1"}


def test_override_replaces_edited_file_for_that_variant():
    spec = {
        **BASE,
        "source_overrides_by_variant": {
            "cuda": [{"filename": "main.py", "content": "print('cuda-edited')"}],
        },
    }
    staged = bov._resolve_staged_sources(spec, "cuda")
    # edited file replaced …
    assert staged["main.py"] == "print('cuda-edited')"
    # … untouched file preserved from base
    assert staged["util.py"] == "X = 1"


def test_override_is_scoped_to_its_variant_only():
    spec = {
        **BASE,
        "source_overrides_by_variant": {
            "cuda": [{"filename": "main.py", "content": "print('cuda-edited')"}],
        },
    }
    # rocm has no override → base source, unaffected by the cuda edit
    assert bov._resolve_staged_sources(spec, "rocm")["main.py"] == "print('hero')"


def test_override_can_add_a_new_file():
    spec = {
        **BASE,
        "source_overrides_by_variant": {
            "cuda": [{"filename": "cuda_kernel.py", "content": "# cuda only"}],
        },
    }
    staged = bov._resolve_staged_sources(spec, "cuda")
    assert staged["cuda_kernel.py"] == "# cuda only"
    assert set(staged) == {"main.py", "util.py", "cuda_kernel.py"}


def test_malformed_override_entries_are_ignored():
    spec = {
        **BASE,
        "source_overrides_by_variant": {
            "cuda": [{"content": "no filename"}, "not-a-dict", {"filename": "ok.py", "content": "1"}],
        },
    }
    staged = bov._resolve_staged_sources(spec, "cuda")
    assert staged["ok.py"] == "1"
    assert "main.py" in staged  # base still present


def test_hero_override_applies_to_hero():
    spec = {
        **BASE,
        "source_overrides_by_variant": {
            "hero": [{"filename": "main.py", "content": "print('hero-edited')"}],
        },
    }
    assert bov._resolve_staged_sources(spec, "hero")["main.py"] == "print('hero-edited')"
