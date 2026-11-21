"""MOD GPU VRAM cap - lazy torch import hook.

Installed into the GPU base images' site-packages as ``sitecustomize.py``
so CPython's ``site`` module imports it automatically at interpreter
startup. It registers a ``sys.meta_path`` finder that intercepts the first
import of ``torch``; after ``torch`` finishes importing it applies
``torch.cuda.set_per_process_memory_fraction(...)`` to every visible
device.

Behavior rules:
  * This module NEVER imports torch itself.
  * No-op unless BOTH env vars are set to positive ints:
        MOD_GPU_VRAM_LIMIT_MB - per-process VRAM cap requested (MB)
        MOD_GPU_CARD_VRAM_MB  - total VRAM of the card (MB)
  * No-op when ``torch.cuda.is_available()`` is false (CPU-only builds).
  * Any exception is swallowed and logged to stderr exactly once; the
    node's ``import torch`` must never break because of this hook.
  * The ROCm build of torch exposes the same ``torch.cuda`` API, so the
    identical code applies to the rocm/intel-xpu images.

Chaining note: if a pre-existing ``sitecustomize.py`` exists elsewhere on
sys.path, Python imports only the first one it finds, so this file
shadows it. Any code a previous sitecustomize provided must be merged
into this file; automatic chaining is deliberately skipped to keep the
hook stdlib-only and side-effect-free.
"""

import os
import sys
import importlib.abc

_LIMIT_ENV = "MOD_GPU_VRAM_LIMIT_MB"
_CARD_ENV = "MOD_GPU_CARD_VRAM_MB"
_LOGGED = False


def _apply_cap(torch_mod):
    """Apply the VRAM fraction to all visible CUDA devices. Never raises."""
    global _LOGGED
    try:
        limit_s = os.environ.get(_LIMIT_ENV)
        card_s = os.environ.get(_CARD_ENV)
        if not limit_s or not card_s:
            return
        limit = int(limit_s)
        card = int(card_s)
        if limit <= 0 or card <= 0:
            return
        cuda = torch_mod.cuda
        if not cuda.is_available():
            return
        fraction = min(1.0, limit / card)
        for dev in range(cuda.device_count()):
            cuda.set_per_process_memory_fraction(fraction, dev)
    except Exception as exc:  # noqa: BLE001 - must never break torch import
        if not _LOGGED:
            _LOGGED = True
            sys.stderr.write(
                "[mod_gpu_cap] VRAM cap hook failed (ignored): %r\n" % (exc,)
            )


class _CapLoader:
    """Wraps the real torch loader and runs the cap after exec_module."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def exec_module(self, module):
        self._inner.exec_module(module)
        _apply_cap(module)


class _TorchCapFinder(importlib.abc.MetaPathFinder):
    """Finds the real ``torch`` spec, wraps its loader, then retires itself."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "torch":
            return None
        for finder in list(sys.meta_path):
            if finder is self:
                continue
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is None:
                continue
            try:
                spec = find_spec(fullname, path, target)
            except Exception:
                spec = None
            if spec is not None:
                try:
                    sys.meta_path.remove(self)
                except ValueError:
                    pass
                if spec.loader is not None:
                    spec.loader = _CapLoader(spec.loader)
                return spec
        return None


def _install():
    if "torch" in sys.modules:
        return  # torch already imported; nothing left to hook
    if any(isinstance(f, _TorchCapFinder) for f in sys.meta_path):
        return
    # Must go BEFORE PathFinder: torch lives in site-packages, so a finder
    # appended at the end of meta_path would never be consulted.
    sys.meta_path.insert(0, _TorchCapFinder())


_install()
