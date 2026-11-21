"""Tests for generate_thumbnail._parse_render_params (THUMBNAIL_SIZE /
BACKGROUND_COLOR wiring) and, where the libs exist, a render-level check.

The container's heavy libs (pyrender, trimesh, cadquery, weasyprint) are NOT
installed on the harness, so they are stubbed in sys.modules before the script
is imported - the same pattern the other container tests use. numpy/PIL are
used real when present (the script imports them at module level); a stub is
only a fallback so the parse tests still run. No DB, no MinIO, no API.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_STUBS = ("pyrender", "trimesh", "cadquery", "weasyprint", "minio", "requests", "pxr")
_stubbed = []
for _name in _STUBS:
    if _name not in sys.modules:
        try:
            __import__(_name)
        except ImportError:
            import types
            sys.modules[_name] = types.ModuleType(_name)
            _stubbed.append(_name)

for _name in ("numpy", "PIL"):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except ImportError:
            import types
            sys.modules[_name] = types.ModuleType(_name)
            _stubbed.append(_name)
if "PIL" in _stubbed:
    sys.modules["PIL"].Image = sys.modules["PIL"]  # `from PIL import Image`

import generate_thumbnail as gt  # noqa: E402

TRANSPARENT = [0, 0, 0, 0]
WHITE = [255, 255, 255, 255]
BLACK = [0, 0, 0, 255]
GRAY = [128, 128, 128, 255]


def _env(size=None, bg=None):
    e = {}
    if size is not None:
        e["THUMBNAIL_SIZE"] = size
    if bg is not None:
        e["BACKGROUND_COLOR"] = bg
    return e


def test_absent_defaults():
    """Absent -> node defaults: 512 / transparent."""
    assert gt._parse_render_params(_env()) == (512, TRANSPARENT)


def test_blank_values():
    """Blank/whitespace behaves like absent (worker skips blank values, but
    a blank env var must not crash either)."""
    assert gt._parse_render_params(_env("", "  ")) == (512, TRANSPARENT)


def test_all_sizes():
    for s in (256, 512, 1024, 2048):
        assert gt._parse_render_params(_env(str(s))) == (s, TRANSPARENT)


def test_all_colors():
    for name, rgba in (("white", WHITE), ("black", BLACK), ("gray", GRAY),
                       ("transparent", TRANSPARENT)):
        assert gt._parse_render_params(_env(bg=name)) == (512, rgba)
        assert gt._parse_render_params(_env(bg=name.upper())) == (512, rgba)


def test_invalid_size_falls_back():
    """Invalid size -> 512, never a crash."""
    assert gt._parse_render_params(_env("0")) == (512, TRANSPARENT)
    assert gt._parse_render_params(_env("300")) == (512, TRANSPARENT)
    assert gt._parse_render_params(_env("abc")) == (512, TRANSPARENT)
    assert gt._parse_render_params(_env("512.0")) == (512, TRANSPARENT)


def test_invalid_color_falls_back():
    assert gt._parse_render_params(_env(bg="navy")) == (512, TRANSPARENT)
    assert gt._parse_render_params(_env(bg="transparent ")) == (512, TRANSPARENT)


def test_size_and_color_together():
    assert gt._parse_render_params(_env("1024", "white")) == (1024, WHITE)


def test_never_raises():
    for size in ("", " ", "-1", "999999999999999999999", "nan"):
        gt._parse_render_params(_env(size))  # must not raise


def test_parse_function_exists():
    assert callable(gt._parse_render_params)


def test_constants_match_node_schema():
    """The script's defaults follow the node definition's configuration_schema."""
    assert gt.DEFAULT_THUMBNAIL_SIZE == 512
    assert gt.ALLOWED_THUMBNAIL_SIZES == (256, 512, 1024, 2048)
    assert gt.DEFAULT_BACKGROUND_COLOR == "transparent"
    assert set(gt.BACKGROUND_COLORS) == {"transparent", "white", "black", "gray"}


def test_render_level_if_libs():
    """Only runs where pyrender + trimesh really import (skipped otherwise)."""
    if "pyrender" in _stubbed or "trimesh" in _stubbed:
        pytest.skip("pyrender+trimesh not installed on this harness "
                    "(render-level leg needs them)")
    import trimesh
    mesh = trimesh.creation.box()
    img = gt.render_thumbnail(mesh, width=256, height=256, bg_color=TRANSPARENT)
    assert img.size == (256, 256)
    assert img.mode == "RGBA"
    px = img.getpixel((0, 0))
    assert px[3] == 0, f"transparent corner pixel must have alpha 0, got {px}"
