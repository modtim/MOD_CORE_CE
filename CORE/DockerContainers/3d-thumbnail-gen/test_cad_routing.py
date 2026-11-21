"""Routing tests for CAD inputs in generate_thumbnail.load_mesh.

_load_cad_mesh uses cadquery importStep (STEP only), so STEP/STP must route
there and IGES/IGS must be refused with a clear error naming STEP/STP before
any CAD reader runs. No MinIO, no API, no real CAD kernel: _load_cad_mesh is
monkeypatched. Heavy libraries missing on the harness are stubbed below.
"""
import os
import sys

import pytest

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import generate_thumbnail as gt  # noqa: E402


class _Sentinel:
    """Stands in for trimesh.Trimesh so a routed call is observable."""


@pytest.fixture
def cad_calls(monkeypatch):
    calls = []

    def fake_load_cad_mesh(filepath):
        calls.append(filepath)
        return _Sentinel()

    monkeypatch.setattr(gt, "_load_cad_mesh", fake_load_cad_mesh)
    return calls


@pytest.mark.parametrize("ext", ["step", "stp", ".STEP", "Stp"])
def test_step_and_stp_route_to_cad_loader(cad_calls, ext):
    result = gt.load_mesh("/tmp/part.x", ext)
    assert isinstance(result, _Sentinel)
    assert cad_calls == ["/tmp/part.x"]


@pytest.mark.parametrize("ext", ["iges", "igs", ".IGES", "Igs"])
def test_iges_is_refused_with_clear_error(cad_calls, ext):
    with pytest.raises(ValueError) as excinfo:
        gt.load_mesh("/tmp/part.x", ext)
    message = str(excinfo.value)
    assert "IGES" in message
    assert "STEP" in message and "STP" in message
    # The CAD reader must never see an IGES file.
    assert cad_calls == []
