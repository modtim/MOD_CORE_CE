"""Universal variant fan-out (wizard-rebuild §4).

Every mode produces a BuildPlan in §3. §4 fans the plan out across
the platform's accelerator set (CPU + CUDA + ROCm + Intel + NPU) so
the registered Node ships with a per-accelerator image variant. The
WorkflowBuilder palette uses these variants to route a workflow run
to a worker that has matching hardware.

Two public entries:

  variants_for_mode(plan)
      Return the full target variant set for a plan, regardless of
      what hardware the cluster currently has online. Speculative
      variants get built; the §9 speculative-test queue waits for
      matching hardware to come online before running the sample
      test. Image-mode plans whose base image is a known CUDA-baked
      C-extension build (`pytorch/pytorch:*-cuda*`, `nvcr.io/nvidia/
      pytorch:*`, etc.) get non-CUDA variants pre-marked
      `architecturally-incompatible` so we don't waste worker time
      trying to cross-compile something physically incompatible.

  adapt_plan_for_variant(plan, variant)
      Return a NEW BuildPlan with fields mutated to target the
      variant's accelerator: cuda_version flipped, install_steps
      rewritten where torch's pip index URL needs the right tag
      (cu121, rocm5.7, etc.), system_deps adjusted. Pure function —
      input plan is unchanged.

Both functions are exhaustively unit-tested in
test_variant_strategy.py; no LLM, no network, no DB.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal, Optional

from node_builder.build_plan.types import BuildPlan

logger = logging.getLogger(__name__)


# Open-ended accelerator type — any string a worker advertises is a
# valid accelerator. The five canonical names (cpu/cuda/rocm/intel/npu)
# are the ones with curated install recipes / known bases, but new
# accelerator tags brought in by workers (habana, tpu, inferentia,
# trainium, etc.) pass through unfiltered. The wizard's Containerizer
# LLM picks a base image for unknown accelerators using general
# knowledge, and the library_profiler caches the recipe from the
# first successful build for reuse on subsequent runs.
Accelerator = str
Compatibility = Literal["compatible", "architecturally-incompatible"]


@dataclass(slots=True)
class VariantSpec:
    """One target variant to build (or refuse to build) for a Node.

    `key` is the harbor tag suffix — `:latest-cuda`, `:latest-rocm`,
    etc. `:latest` (no suffix) is reserved for hero, which is whichever
    variant the wizard picked.

    `compatibility` lets the variant strategy refuse cross-compiles
    up front. When it's `architecturally-incompatible`, the build
    pipeline skips this variant entirely; the Node card surfaces
    `incompatibility_reason` so the user knows why.
    """
    key: str
    accelerator: Accelerator
    cuda_version: Optional[str] = None
    label: str = ""
    compatibility: Compatibility = "compatible"
    incompatibility_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Variant matrix — the universal target set the wizard fans out to
# ---------------------------------------------------------------------------

# Default CUDA version for plans that don't pin one. Matches the
# only currently-published CUDA tag in TEMPLATE_BASE_MATRIX (renderer.py).
# Bumped from 12.1 → 12.4 on 2026-05-23 after `docker manifest inspect`
# confirmed 12.1 / 11.8 were missing from harbor.
_DEFAULT_CUDA_VERSION = "12.4"


def _base_variant_set(cuda_version: Optional[str]) -> list[VariantSpec]:
    """Produce the universal target variant set. Caller adapts each
    VariantSpec for plan-specific concerns (compatibility marks,
    etc.).
    """
    cuda = cuda_version or _DEFAULT_CUDA_VERSION
    return [
        VariantSpec(key="cpu", accelerator="cpu", label="CPU"),
        VariantSpec(
            key="cuda", accelerator="cuda",
            cuda_version=cuda, label=f"CUDA {cuda}",
        ),
        VariantSpec(key="rocm", accelerator="rocm", label="ROCm"),
        VariantSpec(key="intel", accelerator="intel", label="Intel"),
        VariantSpec(key="npu", accelerator="npu", label="NPU"),
    ]


# ---------------------------------------------------------------------------
# C-extension heuristic — Image mode refuses cross-compile up front
# ---------------------------------------------------------------------------

# Image refs that ship CUDA-baked `.so` files (libcuda / libcudnn /
# libtorch_cuda.so). Cross-compiling these to ROCm or Intel is
# physically impossible — the binaries are arch-pinned at the layer
# level. Name-based first pass; v2 could peek into manifest layer
# configs to confirm by scanning for libcuda symbols.
_C_EXTENSION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"nvcr\.io/nvidia/(pytorch|tensorflow|cuda|cudnn|tensorrt)", re.IGNORECASE),
        "NVIDIA NGC image — CUDA-baked C extensions can't cross-compile to ROCm/Intel/NPU.",
    ),
    (
        re.compile(r"^pytorch/pytorch:[^-]+-cuda\d+", re.IGNORECASE),
        "pytorch/pytorch image with CUDA tag — torch's CUDA backend is compiled in.",
    ),
    (
        re.compile(r"^tensorflow/tensorflow:.*(?:-gpu|-cuda)", re.IGNORECASE),
        "tensorflow with GPU/CUDA tag — TF's CUDA kernels are compiled in.",
    ),
    (
        re.compile(r"(?:^|/)cudnn|/cuda(?:-base)?:|^cuda:", re.IGNORECASE),
        "CUDA / cuDNN base image — kernels are CUDA-only by design.",
    ),
    (
        re.compile(r"tensorrt", re.IGNORECASE),
        "TensorRT image — NVIDIA-only inference engine, no ROCm/Intel equivalent.",
    ),
    (
        re.compile(r"huggingface/transformers-pytorch-gpu", re.IGNORECASE),
        "HuggingFace transformers GPU image — torch's CUDA backend is baked in.",
    ),
)


def is_likely_c_extension_image(image_ref: str) -> tuple[bool, Optional[str]]:
    """Return (is_likely_c_ext, reason).

    True when `image_ref` matches one of the known CUDA-baked image
    patterns. Caller marks non-CUDA variants as
    `architecturally-incompatible` and skips them.

    False does NOT mean "safe to cross-compile" — it just means we
    don't have a strong-enough signal to refuse up front. The
    per-variant build will discover incompatibility the slow way.
    """
    if not image_ref:
        return (False, None)
    for pattern, reason in _C_EXTENSION_PATTERNS:
        if pattern.search(image_ref):
            return (True, reason)
    return (False, None)


# ---------------------------------------------------------------------------
# Public — variants_for_mode
# ---------------------------------------------------------------------------

def variants_for_mode(plan: BuildPlan) -> list[VariantSpec]:
    """Return the full variant set for a plan.

    For Image-mode plans wrapping a CUDA-baked base, non-CUDA
    variants come back marked `architecturally-incompatible` with a
    human-readable reason — the build pipeline must skip those.

    For all other modes (git / script / dockerfile / describe-codegen)
    and for Image mode with a non-CUDA base, every variant in the
    universal set is `compatible`. The §4 plan adapter handles the
    per-variant install-step rewrites; cross-compile failures only
    surface at the per-variant sample-test stage in §5.

    Note: this function is hardware-state-agnostic — it returns the
    set the wizard WANTS to target. Cluster hardware-online filtering
    happens later in `node_builder/build_dag/generator.py` (which
    can defer non-matching variants to the §9 speculative queue).
    """
    variants = _base_variant_set(plan.cuda_version)

    if plan.mode == "image" and plan.from_image_ref:
        is_c_ext, reason = is_likely_c_extension_image(plan.from_image_ref)
        if is_c_ext:
            return [
                v if v.accelerator == "cuda"
                else _mark_incompatible(v, reason or "C-extension image")
                for v in variants
            ]

    # Library-profile-based restriction. Inspects script imports +
    # passthrough Dockerfile + install_steps via the library profiler;
    # if any library has a specific accelerator requirement (e.g.
    # nerfstudio → CUDA), variants for OTHER accelerators are marked
    # incompatible up front so we don't waste build time on
    # cross-compiles that physically can't succeed.
    try:
        restriction = _detect_library_accelerator_restriction(plan)
    except Exception as e:  # noqa: BLE001
        # Defensive: profiler failures must NEVER block the build
        # pipeline. Log and proceed with the unrestricted variant set.
        logger.warning(
            "variants_for_mode: library restriction check failed (%s); "
            "proceeding with unrestricted variants",
            e,
        )
        restriction = None

    if restriction:
        required_accel, reason, _libs = restriction
        if required_accel == "impossible":
            # No single deployable variant works — mark ALL incompatible.
            # The build pipeline will surface the reason to the user
            # instead of attempting and failing.
            return [_mark_incompatible(v, reason) for v in variants]
        # Restrict to the single required accelerator.
        return [
            v if v.accelerator == required_accel
            else _mark_incompatible(v, reason)
            for v in variants
        ]

    # No hard library restriction. But if NOTHING in the plan can actually use a
    # GPU (no device-abstracting framework like torch, no GPU-requiring lib),
    # then every accelerator variant would be byte-identical CPU code with the
    # GPU sitting idle — a waste to build AND it misroutes the node to the
    # ACCELERATOR queue (which CPU-only workers don't poll). Restrict to CPU:
    # the CPU node still runs on GPU workers via the universal CPU queue.
    # Conservative — only fires when we can see the code has recognizable,
    # non-GPU-capable libs (never strips a real GPU variant).
    if not _plan_has_gpu_capable_library(plan):
        return [
            v if v.accelerator == "cpu"
            else _mark_incompatible(
                v, "no GPU-accelerated dependency — CPU-only (still runs on GPU "
                   "workers via the CPU queue)")
            for v in variants
        ]

    return variants


def _plan_has_gpu_capable_library(plan: BuildPlan) -> bool:
    """True if the plan pulls in ≥1 library that can actually USE a GPU — a
    device-abstracting framework (torch/onnxruntime/tf → `is_multi_target`) or
    one that requires a GPU accelerator. False only when we can SEE the deps and
    none are GPU-capable. Returns True (don't restrict) whenever we can't tell —
    Image mode, git repos with no pip-visible libs, empty plans — so a genuine
    GPU node is never wrongly downgraded to CPU-only.
    """
    pieces: list[str] = []
    if getattr(plan, "script_body", None):
        pieces.append(plan.script_body)
    if getattr(plan, "passthrough_dockerfile", None):
        pieces.append(plan.passthrough_dockerfile)
    if getattr(plan, "install_steps", None):
        pieces.extend(plan.install_steps)
    text = "\n".join(pieces)
    if not text.strip():
        return True  # can't see deps (e.g. Image mode) → don't restrict

    try:
        from node_builder.gpu_library_index import extract_package_candidates, is_multi_target
        candidates = extract_package_candidates(text)
        if not candidates:
            return True  # nothing recognizable → be safe, keep variants
        from services.library_profiler import profile as _profile_lib
        _GPU_ACCELS = {"CUDA", "ROCM", "INTEL", "NPU", "XPU", "TPU",
                       "HABANA", "IPU", "INFERENTIA", "TRAINIUM", "MPS"}
        for raw in candidates:
            if is_multi_target(raw):
                return True
            res = _profile_lib(raw)
            res_u = str(res).upper() if res else "UNKNOWN"
            if res_u in _GPU_ACCELS:
                return True
            if res_u != "CPU":
                # Profiler couldn't classify this dep (UNKNOWN / 404 /
                # network miss). We can't tell whether it's GPU-capable,
                # so per contract don't restrict — never downgrade a
                # possibly-GPU node to CPU-only on incomplete evidence.
                return True
        return False
    except Exception as e:  # noqa: BLE001 — must never block the build
        logger.warning("gpu-capable-library check failed (%s); keeping variants", e)
        return True


def _mark_incompatible(v: VariantSpec, reason: str) -> VariantSpec:
    """Return a copy of `v` flagged as architecturally-incompatible."""
    return VariantSpec(
        key=v.key, accelerator=v.accelerator,
        cuda_version=v.cuda_version, label=v.label,
        compatibility="architecturally-incompatible",
        incompatibility_reason=reason,
    )


# ---------------------------------------------------------------------------
# Library-profile-based restriction
# ---------------------------------------------------------------------------

# Maps the profiler's accelerator codes (UPPER) to the variant_strategy
# accelerator literal (lower). The wizard's VALID_PROCESS_TYPES space
# is a subset of the profiler's recognised types — the profiler also
# knows TPU/HABANA/IPU/INFERENTIA/TRAINIUM/MPS which we can't deploy.
_PROFILER_TO_VARIANT_ACCEL: dict[str, str] = {
    "CPU": "cpu",
    "CUDA": "cuda",
    "ROCM": "rocm",
    "INTEL": "intel",
    "NPU": "npu",
}


def _detect_library_accelerator_restriction(plan: BuildPlan) -> Optional[tuple[str, str, list[str]]]:
    """Inspect the plan's script_body + passthrough_dockerfile +
    install_steps for library references, look each up in the
    library profiler, and return a single accelerator restriction
    if ANY non-multi-target library has a specific requirement.

    Returns ``(required_accelerator, reason, triggering_libraries)``
    or ``None`` when no restriction applies (everything is
    multi-target or UNKNOWN).

    Conflict handling: if two libraries demand different non-deployable
    accelerators (e.g. nerfstudio→CUDA AND habana_frameworks→HABANA),
    we cannot build a single variant that satisfies both. Returns a
    pseudo-accelerator code ``"impossible"`` with a reason listing
    the conflict — caller marks ALL variants incompatible.
    """
    # Compose the searchable text from every plan field that might
    # carry library references.
    pieces: list[str] = []
    if plan.script_body:
        pieces.append(plan.script_body)
    if plan.passthrough_dockerfile:
        pieces.append(plan.passthrough_dockerfile)
    if plan.install_steps:
        pieces.extend(plan.install_steps)
    text = "\n".join(pieces)

    from node_builder.gpu_library_index import (
        extract_package_candidates,
        is_multi_target,
    )
    candidates = extract_package_candidates(text)
    if not candidates:
        return None

    from services.library_profiler import profile as _profile_lib

    # Group triggering libs by the accelerator they require. Multi-
    # target libs are skipped (they don't restrict). UNKNOWN / CPU
    # results don't restrict either — only specific accelerator hits.
    by_accel: dict[str, list[str]] = {}
    for raw in candidates:
        if is_multi_target(raw):
            continue
        result = _profile_lib(raw)
        if not result or result in ("CPU", "UNKNOWN"):
            continue
        by_accel.setdefault(result, []).append(raw)

    if not by_accel:
        return None

    if len(by_accel) > 1:
        # Conflict — multiple libs demand different accelerators.
        # No single deployable variant can satisfy all.
        parts = [f"{libs[0]} requires {accel}" for accel, libs in by_accel.items()]
        reason = (
            "Imports demand mutually-incompatible accelerators: "
            + "; ".join(parts) + ". Cannot build a single variant."
        )
        triggering = [lib for libs in by_accel.values() for lib in libs]
        return ("impossible", reason, triggering)

    # Exactly one accelerator demanded. If it's deployable, restrict
    # variants to that accelerator only. If it's not deployable
    # (TPU/HABANA/IPU/INFERENTIA/TRAINIUM/MPS), all deployable variants
    # are incompatible — same "impossible" code with a different reason.
    accel, libs = next(iter(by_accel.items()))
    if accel in _PROFILER_TO_VARIANT_ACCEL:
        variant_accel = _PROFILER_TO_VARIANT_ACCEL[accel]
        reason = (
            f"{libs[0]} requires {accel} at runtime — only the "
            f"{variant_accel.upper()} variant is buildable."
        )
        return (variant_accel, reason, libs)

    reason = (
        f"{libs[0]} requires {accel} accelerator at runtime — "
        f"platform does not currently deploy {accel}. No variant "
        f"is buildable until a {accel} base image is available."
    )
    return ("impossible", reason, libs)


# ---------------------------------------------------------------------------
# Public — adapt_plan_for_variant
# ---------------------------------------------------------------------------

# Torch wheel index URLs per accelerator. Used to rewrite
# `pip install torch …` lines so each variant pulls the right
# pre-built wheel rather than building from source.
_TORCH_INDEX_URLS: dict[str, str] = {
    "cuda11.8": "https://download.pytorch.org/whl/cu118",
    "cuda12.1": "https://download.pytorch.org/whl/cu121",
    "cuda12.4": "https://download.pytorch.org/whl/cu124",
    "rocm":     "https://download.pytorch.org/whl/rocm5.7",
    "cpu":      "https://download.pytorch.org/whl/cpu",
}

# Match a torch pip install — captures the existing index-url flag
# (if any) so we can replace it cleanly.
_TORCH_INSTALL_RE = re.compile(
    r"\b(pip3?\s+install\s+(?:--?\S+\s+)*(?:torch|torchvision|torchaudio))",
    re.IGNORECASE,
)
_EXISTING_INDEX_URL_RE = re.compile(
    r"--?(?:extra-)?index-url[=\s]\S+",
    re.IGNORECASE,
)


def adapt_plan_for_variant(plan: BuildPlan, variant: VariantSpec) -> BuildPlan:
    """Return a new BuildPlan mutated to target `variant`. Input plan
    is unchanged (deep-copies via Pydantic).

    Universal mutations applied to all modes:
      - `cuda_version` set from variant (None for cpu/rocm/intel/npu)
      - install_steps that pip-install torch rewritten to use the
        right `--index-url` (cu121 → rocm5.7 → cpu, etc.)

    Image mode: no plan mutation; the renderer's FROM line stays put.
    Cross-architecture compatibility is the C-extension heuristic's
    job — by the time `adapt_plan_for_variant` runs, an incompatible
    Image-mode variant has already been filtered to compatibility=
    architecturally-incompatible by `variants_for_mode`.

    Caller is expected to skip incompatible variants — this function
    doesn't refuse to adapt them (it's the renderer's caller's job
    to short-circuit).
    """
    adapted = plan.model_copy(deep=True)

    # Set cuda_version for CUDA variants; clear it for others so the
    # renderer's TEMPLATE_BASE_MATRIX picks the CPU/ROCm/etc. row.
    if variant.accelerator == "cuda":
        adapted.cuda_version = variant.cuda_version or _DEFAULT_CUDA_VERSION
        adapted.gpu_required = True
    else:
        adapted.cuda_version = None
        # rocm/intel/npu are still GPUs in the platform's mental
        # model; cpu is the only one that's truly CPU.
        adapted.gpu_required = variant.accelerator != "cpu"

    # Rewrite torch install steps. Only relevant for Python plans
    # that install torch via pip (script / git / describe-codegen
    # standard pipeline); Image mode's install_steps are empty.
    if plan.install_steps and plan.language == "python":
        adapted.install_steps = [
            _rewrite_torch_step(step, variant) for step in plan.install_steps
        ]

    return adapted


def apply_variant_fanout(spec, plan) -> None:
    """Populate a BuildSpec's hero + variant + incompatible slots
    from a BuildPlan (wizard-rebuild §4 chunk 4).

    Composition of `variants_for_mode` + `adapt_plan_for_variant` +
    `render_artifacts`. After this call:

      spec.hero_dockerfile / entrypoint_sh / build_and_push_sh  ← the
          rendered hero variant's artifacts. Hero picking rule: cuda
          when only CUDA is compatible (C-ext image case); cpu
          otherwise (so the Node is broadly runnable on any worker).
      spec.hero_variant  ← the hero's accelerator key.
      spec.variants      ← BuildVariantSpec for every OTHER
                            compatible variant, with per-accelerator
                            adapted Dockerfile (torch wheels
                            rewritten via _rewrite_torch_step).
      spec.incompatible_variants ← list of {variant, accelerator,
                            reason} for variants the C-ext
                            heuristic refused up front. Surfaces on
                            the Node card.

    Idempotent: callers can re-run after plan-aware repair (§7)
    mutates the plan; previous variants are overwritten.

    Raises:
      RuntimeError if the variant set has no compatible variants
      (caller is expected to surface as 500 — the C-ext heuristic
      shouldn't ever produce zero compatible variants in practice).
    """
    from node_builder.build_dag.types import BuildVariantSpec
    from node_builder.build_plan.renderer import render_artifacts

    variants = variants_for_mode(plan)
    compatible = [
        v for v in variants if v.compatibility == "compatible"
    ]
    incompatible = [
        v for v in variants if v.compatibility != "compatible"
    ]
    if not compatible:
        raise RuntimeError(
            "variant fan-out: no compatible variants for plan — "
            "C-ext heuristic refused every accelerator"
        )

    # Hero picking rule. When only CUDA is compatible (Image-mode
    # CUDA-baked base), hero must be CUDA. Otherwise cpu — broadest
    # runtime compatibility, matches the legacy register_node
    # default.
    all_cuda = all(v.accelerator == "cuda" for v in compatible)
    hero_key = "cuda" if all_cuda else "cpu"
    hero = next(
        (v for v in compatible if v.accelerator == hero_key),
        compatible[0],
    )

    # Hero — adapted + rendered.
    hero_adapted = adapt_plan_for_variant(plan, hero)
    hero_artifacts = render_artifacts(hero_adapted)
    spec.hero_dockerfile = hero_artifacts["dockerfile"]
    spec.entrypoint_sh = hero_artifacts["entrypoint_sh"]
    spec.build_and_push_sh = hero_artifacts["build_and_push_sh"]
    spec.hero_variant = hero.key

    # Non-hero compatible variants. Clear any prior list so re-runs
    # don't accumulate.
    spec.variants = []
    for v in compatible:
        if v.key == hero.key:
            continue
        adapted = adapt_plan_for_variant(plan, v)
        rendered = render_artifacts(adapted)
        spec.variants.append(BuildVariantSpec(
            variant=v.key,
            dockerfile=rendered["dockerfile"],
        ))

    # Incompatible variants — persist with reasons for the Node card.
    spec.incompatible_variants = [
        {
            "variant": v.key,
            "accelerator": v.accelerator,
            "reason": v.incompatibility_reason or "incompatible",
        }
        for v in incompatible
    ]


def _rewrite_torch_step(step: str, variant: VariantSpec) -> str:
    """If `step` is a torch pip install, rewrite its --index-url to
    point at the right per-accelerator wheel registry. Other steps
    pass through unchanged."""
    if not _TORCH_INSTALL_RE.search(step):
        return step

    # Pick the right wheel index for the variant.
    if variant.accelerator == "cuda":
        key = f"cuda{(variant.cuda_version or _DEFAULT_CUDA_VERSION).replace('.', '')[:3]}"
        # Map e.g. cuda121 → cuda12.1's URL.
        full_key = f"cuda{variant.cuda_version}" if variant.cuda_version else "cuda12.1"
        index_url = _TORCH_INDEX_URLS.get(full_key) or _TORCH_INDEX_URLS["cuda12.1"]
    elif variant.accelerator == "rocm":
        index_url = _TORCH_INDEX_URLS["rocm"]
    elif variant.accelerator in ("intel", "npu"):
        # No prebuilt torch wheels for intel/npu — fall back to CPU.
        index_url = _TORCH_INDEX_URLS["cpu"]
    else:
        index_url = _TORCH_INDEX_URLS["cpu"]

    # Strip any existing --index-url / --extra-index-url, then
    # append the new one.
    cleaned = _EXISTING_INDEX_URL_RE.sub("", step).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return f"{cleaned} --index-url {index_url}"
