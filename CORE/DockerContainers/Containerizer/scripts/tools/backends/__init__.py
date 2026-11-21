"""Multi-vendor backend support layer (P9-AA / multivendor roadmap).

This package owns the cross-cutting machinery for emitting Dockerfiles +
adapter outputs across non-CUDA backends. The simple-recipe variants
(cuda / rocm / cpu) live in `tools/gpu_variants.py` because they share a
single template family. Anything more involved — Intel XPU via IPEX,
Intel NPU via OpenVINO, future vendor backends — composes the four
public objects below:

    capability_matrix.SUPPORT     # backend × feature truth table
    architectures.IS_ALLOWED      # per-backend HF model allowlist
    negative_classifier.classify  # source → {backend: incompat reason}
    recipes.STUB_RENDERERS        # backend → callable(args) -> Dockerfile

The main entrypoint, `plan_unsupported_variants`, ties them together:
given the source, the platform's enabled-tags whitelist, and the
variants the simple-recipe path emitted, it returns stub artefacts for
the gap. Today the gap is everything outside cuda/rocm/cpu; as Phase 2+
recipes land, the gap shrinks because real recipes start filling those
slots in `recipes/`.

See `docs/MULTIVENDOR_ROADMAP.md` for the architecture and phase plan.
"""

from .capability_matrix import SUPPORT, supports
from .negative_classifier import classify
from .architectures import IS_ALLOWED, is_allowed
from .planner import plan_unsupported_variants

__all__ = [
    "SUPPORT",
    "supports",
    "classify",
    "IS_ALLOWED",
    "is_allowed",
    "plan_unsupported_variants",
]
