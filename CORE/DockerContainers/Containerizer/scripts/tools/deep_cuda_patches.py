"""Deterministic post-processing for LLM-generated Dockerfiles when
the source tree is deep-CUDA (compiled .cu kernels, CUDAExtension
setup.py, git submodules with differentiable-rasterizer-style code).

The LLM does a great job with standard PyTorch/CUDA images but has
no way to know things like "4DGaussians ships diff-gaussian-rasterization
as a git submodule" or "the worker GPU pool spans compute capabilities
7.5 through 9.0". This module deterministically patches those in after
the LLM returns.

Called from create_container.py when DEEP_CUDA_HINTS env var is
present. Idempotent — re-running on an already-patched Dockerfile
is a no-op.

The hints dict is populated by the API's
`routers.node_definitions._detect_deep_cuda_with_hints` and shipped
into the Containerizer via env var (JSON-encoded).
"""
from __future__ import annotations

import re
from typing import Any, Dict


_SUBMODULE_MARKER = "# [deep-cuda] git submodules"
_ARCH_MARKER = "# [deep-cuda] arch envs"


# Per-variant arch-env blocks. The CUDA variant wants TORCH_CUDA_ARCH_LIST
# + FORCE_CUDA + CUDA_HOME. ROCm wants PYTORCH_ROCM_ARCH + ROCM_HOME.
# Intel wants SYCL_TARGETS. NPU ignores arch lists entirely.
_DEFAULT_ROCM_ARCH = "gfx90a;gfx942;gfx1100;gfx1201"
_DEFAULT_SYCL_TARGETS = "intel_gpu_pvc,intel_gpu_dg2"


def apply_deep_cuda_patches(
    dockerfile: str,
    hints: Dict[str, Any],
    *,
    variant: str = "cuda",
) -> str:
    """Patch a Dockerfile text with deep-CUDA one-click build hints.

    Accepts: the raw Dockerfile string the LLM emitted, plus the hints
    dict built in the API's /predict-variants detector, plus (Phase 9)
    the target variant so arch envs come out right for non-CUDA
    backends.
    Returns: a patched Dockerfile with:
      - CUDA: ENV TORCH_CUDA_ARCH_LIST + FORCE_CUDA=1 + CUDA_HOME
      - ROCm: ENV PYTORCH_ROCM_ARCH + ROCM_HOME
      - Intel: ENV SYCL_TARGETS
      - NPU / Metal / CPU: no arch envs
      - All: RUN git submodule update --init --recursive (when
        needs_submodule_init is set; submodule content is needed
        regardless of target backend).

    Insertion strategy: both patches slot in AFTER the first `WORKDIR`
    line (which the wizard's Albedo Dockerfile template always emits)
    and BEFORE the first `RUN pip install` / `RUN python` / `COPY
    entrypoint.sh`. That window is where the LLM would put build-prep
    steps if it knew about them.

    Idempotent: scans for marker comments before inserting so
    re-running (e.g. the Containerizer retries on transient LLM
    failure) doesn't double-apply.

    `variant` defaults to "cuda" so callers that existed before the
    Phase 9 rework keep emitting the CUDA arch list (back-compat).
    """
    if not hints or not dockerfile:
        return dockerfile

    cuda_arch = (hints.get("cuda_arch_list") or "").strip()
    needs_submodule = bool(hints.get("needs_submodule_init"))

    if not cuda_arch and not needs_submodule:
        return dockerfile

    lines = dockerfile.splitlines(keepends=False)

    patches: list[str] = []

    # Arch envs — variant-dependent. Each variant's own porter may
    # additionally emit the same envs via its own patches; that's
    # tolerated (marker makes this pass idempotent regardless).
    if cuda_arch and _ARCH_MARKER not in dockerfile:
        arch_block = _arch_block_for_variant(variant, cuda_arch)
        if arch_block:
            patches.append(_ARCH_MARKER)
            patches.extend(arch_block)

    # Submodule init — only if .gitmodules was detected. Tolerant of
    # network flakes via || true so a failed submodule init doesn't
    # sink the whole build; the subsequent pip install will fail
    # visibly with a clear "module not found" error if the submodule
    # contents were actually needed.
    if needs_submodule and _SUBMODULE_MARKER not in dockerfile:
        patches.append(_SUBMODULE_MARKER)
        # `git config --global --add safe.directory '*'` covers the
        # common case where the container runs git as root but the
        # checkout was staged by a non-root user — modern git refuses
        # to operate on repos it considers "dubious ownership".
        patches.append("RUN git config --global --add safe.directory '*' \\")
        patches.append("    && (test -f .gitmodules && git submodule update --init --recursive || true)")

    if not patches:
        return dockerfile

    # Find insertion point. Prefer "after first WORKDIR, before first
    # RUN pip / RUN python / COPY entrypoint". Fall back to "after
    # first FROM" if the Dockerfile doesn't have a WORKDIR (it should,
    # but be defensive).
    insert_at = _find_insertion_index(lines)

    # Splice patches in with a leading blank line so the diff is
    # visible when the wizard preview renders.
    before = lines[:insert_at]
    after = lines[insert_at:]
    merged = before + [""] + patches + [""] + after
    return "\n".join(merged) + ("\n" if dockerfile.endswith("\n") else "")


def _find_insertion_index(lines: list[str]) -> int:
    """Pick the line index to insert the patch block at.

    Strategy:
      1. After the last WORKDIR line (the wizard template keeps this
         near the top; multiple WORKDIRs are rare but we pick the
         last to sit right before the build-prep steps).
      2. Before the first RUN pip / RUN python / RUN apt / COPY
         entrypoint — the LLM puts build-deps after those, so we
         need to land BEFORE them.
      3. Falls back to right after the first FROM line if neither
         WORKDIR nor a recognisable RUN/COPY appears.
    """
    last_workdir = -1
    first_run_pip = -1
    first_copy_entrypoint = -1
    first_from = -1
    run_pip_pat = re.compile(r"^\s*RUN\s+(pip|python|apt|bash|sh|wget|curl)\b", re.I)
    copy_ep_pat = re.compile(r"^\s*COPY\s+.*entrypoint", re.I)
    workdir_pat = re.compile(r"^\s*WORKDIR\s+", re.I)
    from_pat = re.compile(r"^\s*FROM\s+", re.I)

    for i, ln in enumerate(lines):
        if first_from < 0 and from_pat.match(ln):
            first_from = i
        if workdir_pat.match(ln):
            last_workdir = i
        if first_run_pip < 0 and run_pip_pat.match(ln):
            first_run_pip = i
        if first_copy_entrypoint < 0 and copy_ep_pat.match(ln):
            first_copy_entrypoint = i

    # Candidates: land after WORKDIR, but never past the first RUN /
    # COPY entrypoint.
    anchor = last_workdir if last_workdir >= 0 else first_from
    if anchor < 0:
        # Dockerfile is malformed (no FROM?). Drop patches at index 0
        # so the build fails with a clear "FROM missing" rather than
        # silently skipping the fix.
        return 0

    insert = anchor + 1
    # Clamp to "before first RUN pip / COPY entrypoint".
    for limit in (first_run_pip, first_copy_entrypoint):
        if 0 <= limit < insert:
            insert = limit
    return insert


def _arch_block_for_variant(variant: str, cuda_arch: str) -> list[str]:
    """Return the arch-related ENV lines for a target variant.

    CUDA: full CUDA arch list + FORCE_CUDA + CUDA_HOME (legacy behavior).
    ROCm: translate to PYTORCH_ROCM_ARCH. FORCE_CUDA is meaningless on
          ROCm; CUDA_HOME doesn't exist.
    Intel: translate to SYCL_TARGETS. No analog of FORCE_CUDA.
    NPU / Metal / CPU: no arch envs (backend ignores compute caps).

    Unknown variant → treat as CUDA to preserve Phase 8d-2 back-compat.
    """
    v = (variant or "cuda").lower()
    if v == "cuda":
        return [
            f'ENV TORCH_CUDA_ARCH_LIST="{cuda_arch}"',
            "ENV FORCE_CUDA=1",
            "ENV CUDA_HOME=/usr/local/cuda",
        ]
    if v == "rocm":
        return [
            f'ENV PYTORCH_ROCM_ARCH="{_DEFAULT_ROCM_ARCH}"',
            "ENV ROCM_HOME=/opt/rocm",
        ]
    if v == "intel":
        return [
            f'ENV SYCL_TARGETS="{_DEFAULT_SYCL_TARGETS}"',
        ]
    if v in ("npu", "metal", "cpu"):
        return []
    # Unknown → CUDA fallback.
    return [
        f'ENV TORCH_CUDA_ARCH_LIST="{cuda_arch}"',
        "ENV FORCE_CUDA=1",
        "ENV CUDA_HOME=/usr/local/cuda",
    ]
