"""Tests for the MOD GPU VRAM cap sitecustomize hook (fake torch, no GPU)."""

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

SITECUSTOMIZE_PATH = Path(__file__).resolve().parent / "sitecustomize.py"

FAKE_TORCH_INIT = textwrap.dedent(
    """
    import os

    class _FakeCUDA:
        calls = []

        def is_available(self):
            return os.environ.get("_FAKE_TORCH_NO_CUDA", "0") != "1"

        def device_count(self):
            return 2

        def set_per_process_memory_fraction(self, fraction, device=None):
            if os.environ.get("_FAKE_TORCH_RAISE", "0") == "1":
                raise RuntimeError("boom: fake set_per_process_memory_fraction")
            type(self).calls.append((fraction, device))

    cuda = _FakeCUDA()
    """
)


def _load_sitecustomize():
    spec = importlib.util.spec_from_file_location(
        "mod_gpu_cap_sitecustomize_under_test", SITECUSTOMIZE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _purge_torch():
    for name in list(sys.modules):
        if name == "torch" or name.startswith("torch."):
            del sys.modules[name]


def _purge_hook(finder_cls):
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, finder_cls)]


@pytest.fixture
def fake_torch(tmp_path):
    pkg = tmp_path / "torch"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(FAKE_TORCH_INIT)
    sys.path.insert(0, str(tmp_path))
    _purge_torch()
    hook = _load_sitecustomize()
    yield hook
    _purge_torch()
    if str(tmp_path) in sys.path:
        sys.path.remove(str(tmp_path))
    _purge_hook(hook._TorchCapFinder)


def test_hook_does_not_import_torch(fake_torch):
    assert "torch" not in sys.modules


def test_fraction_applied_per_device(fake_torch, monkeypatch):
    monkeypatch.setenv("MOD_GPU_VRAM_LIMIT_MB", "4096")
    monkeypatch.setenv("MOD_GPU_CARD_VRAM_MB", "16384")
    import torch

    assert torch.cuda.calls == [(0.25, 0), (0.25, 1)]


def test_no_call_when_env_unset(fake_torch, monkeypatch):
    monkeypatch.delenv("MOD_GPU_VRAM_LIMIT_MB", raising=False)
    monkeypatch.delenv("MOD_GPU_CARD_VRAM_MB", raising=False)
    import torch

    assert torch.cuda.calls == []


def test_no_call_when_cuda_unavailable(fake_torch, monkeypatch):
    monkeypatch.setenv("MOD_GPU_VRAM_LIMIT_MB", "4096")
    monkeypatch.setenv("MOD_GPU_CARD_VRAM_MB", "16384")
    monkeypatch.setenv("_FAKE_TORCH_NO_CUDA", "1")
    import torch

    assert torch.cuda.calls == []


def test_limit_above_card_capped_at_one(fake_torch, monkeypatch):
    monkeypatch.setenv("MOD_GPU_VRAM_LIMIT_MB", "32768")
    monkeypatch.setenv("MOD_GPU_CARD_VRAM_MB", "16384")
    import torch

    assert torch.cuda.calls == [(1.0, 0), (1.0, 1)]


def test_raising_set_fraction_does_not_break_import(fake_torch, monkeypatch):
    monkeypatch.setenv("MOD_GPU_VRAM_LIMIT_MB", "4096")
    monkeypatch.setenv("MOD_GPU_CARD_VRAM_MB", "16384")
    monkeypatch.setenv("_FAKE_TORCH_RAISE", "1")
    import torch

    assert torch.cuda.is_available() is True
    assert torch.cuda.calls == []
