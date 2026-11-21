"""Curated index of Python libraries whose runtime requires a GPU.

When the analyzer LLM declares ``process_type=CPU`` but the script's
imports include a library from this index, we override to CUDA (or
ROCm where applicable) — and surface the reason to the user so they
can correct if needed.

Why this exists: the analyzer LLM's rule is "torch/tensorflow/cupy/
etc. → CUDA, otherwise CPU." But many high-level libraries depend on
CUDA-only kernels without naming a low-level GPU dep. ``nerfstudio``
imports were the original repro: the analyzer saw ``import
nerfstudio`` and defaulted to CPU. Nerfstudio cannot run without a
CUDA-capable GPU — every backend (nerfacto, instant-ngp, gaussian-
splatting) requires it.

This is a FACT layer, not an LLM layer. We do not try to predict GPU
requirement — the libraries either ship CPU fallbacks or they don't.
Add entries here as we learn them. Conservative bias: only list
libraries with NO usable CPU path (importing fails, or runs
impractically slowly such that no user would pick CPU on purpose).

Entries are matched against the script's imports — both top-level
package and submodule references. Substring matching on the
top-level package is sufficient; we don't try to parse import
statements precisely.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)


# Accelerator types the profiler knows about. The first five
# (CPU/CUDA/ROCM/INTEL/NPU) match the wizard's VALID_PROCESS_TYPES
# in analyze_prompt.py — these are types we can actually DEPLOY.
# The rest are types we can RECOGNISE but can't deploy yet (no base
# image) — the wizard surfaces a clarifying question for those
# instead of pretending CPU will work.
ACCELERATOR_TYPES = (
    "CPU",
    "CUDA",       # NVIDIA — deployable
    "ROCM",       # AMD — deployable
    "INTEL",      # Intel Xe / Arc / Data Center GPU — deployable
    "NPU",        # Intel NPU (and others under same umbrella) — deployable
    "TPU",        # Google Cloud TPU — recognise only
    "HABANA",     # Intel Habana Gaudi — recognise only
    "IPU",        # Graphcore IPU — recognise only
    "INFERENTIA", # AWS Inferentia — recognise only
    "TRAINIUM",   # AWS Trainium — recognise only
    "MPS",        # Apple Silicon Metal Performance Shaders — recognise only
    "UNKNOWN",
)

DEPLOYABLE_TYPES = frozenset({"CPU", "CUDA", "ROCM", "INTEL", "NPU"})


# Libraries that REQUIRE CUDA at runtime. Importing them on a
# CPU-only machine either fails outright or refuses to do anything
# useful. Listed with their canonical PyPI / repo name.
#
# Audit notes per entry — for future contributors deciding whether
# to add a new library: only add when CPU is genuinely impossible
# or so slow that no user would pick it on purpose.
CUDA_REQUIRED: FrozenSet[str] = frozenset({
    # NeRF / 3D-reconstruction frameworks — all require CUDA, no CPU path.
    "nerfstudio",
    "nerfacc",
    "ngp_pl",
    "instant_ngp",
    "instant-ngp",
    "nerfacto",
    "tinycudann",
    "tiny_cuda_nn",
    "gsplat",
    "gaussian_splatting",
    "gaussian-splatting",
    "diff_gaussian_rasterization",
    "simple_knn",

    # 3D / graphics — CUDA kernels only.
    "pytorch3d",
    "kaolin",
    "open3d_ml_torch",
    "nvdiffrast",
    "nvdiffrec",

    # Detection / segmentation — CUDA-only ops.
    "detectron2",
    "mmcv_full",
    "mmdet",
    "mmsegmentation",

    # LLM inference / training — CUDA-only kernels.
    "vllm",
    "deepspeed",
    "flash_attn",
    "flash-attn",
    "xformers",
    "bitsandbytes",
    "triton",
    "gptq",
    "auto_gptq",
    "autoawq",
    "exllama",
    "exllamav2",

    # Low-level CUDA bindings — CUDA by definition.
    "pycuda",
    "cupy",
    "cupyx",

    # Rendering / physics — CUDA kernels only.
    "mitsuba",  # Mitsuba 3 has CUDA + LLVM, but pip default is CUDA
    "warp_lang",
    "warp-lang",

    # Photogrammetry / Structure-from-Motion / 3D vision — these have
    # nominal CPU paths but practical use is GPU-only (CPU runs are
    # measured in hours or days for a single scene). Added for the
    # photogrammetry-node repro: scripts that wrap COLMAP/OpenMVS via
    # Python bindings used to classify CPU because the LLM didn't see
    # torch / cupy / etc.
    "pycolmap",     # COLMAP bindings — CUDA SIFT + dense
    "dust3r",       # DUSt3R: GPU transformer SfM, CPU impractical
    "mast3r",       # MASt3R: same family as DUSt3R
    "pixsfm",       # pixel-perfect-sfm — feature refinement on GPU
    "hloc",         # Hierarchical-Localization — SuperPoint+SuperGlue, GPU only in practice
    "splatfacto",   # nerfstudio gaussian splatting variant
})


# Libraries that REQUIRE ROCm. Rare. Listed for completeness.
ROCM_REQUIRED: FrozenSet[str] = frozenset({
    "pytorch_rocm",
    "tensorflow_rocm",
})


# Intel XPU (Xe / Arc / Data Center GPU) — Intel's GPU acceleration.
# Multi-target libraries (oneAPI, oneDNN) NOT included because they
# can run on Intel CPU, GPU, or NPU depending on config. Only listing
# packages that are specifically Intel-GPU-targeted.
INTEL_REQUIRED: FrozenSet[str] = frozenset({
    "intel_extension_for_pytorch",
    "intel-extension-for-pytorch",
    "intel_pytorch_extension",
    "ipex",
})


# Intel NPU (and other NPU runtimes that compile to Intel NPU silicon).
# Note: OpenVINO itself is NOT here — it's multi-target (CPU/GPU/NPU).
# Only NPU-EXCLUSIVE libraries.
NPU_REQUIRED: FrozenSet[str] = frozenset({
    "intel_npu_acceleration_library",
})


# Google Cloud TPU. We don't deploy to TPU yet — the profiler still
# recognises so the wizard can surface a clear "we don't support TPU
# yet" instead of falling back to CPU silently.
TPU_REQUIRED: FrozenSet[str] = frozenset({
    "libtpu",
    "tpu_estimator",
})


# Intel Habana Gaudi (HPU). Distinct from Intel XPU.
HABANA_REQUIRED: FrozenSet[str] = frozenset({
    "habana_frameworks",
    "habana_torch_loader",
    "habana_torch_dataloader",
    "habana_torch_plugin",
    "habana_dataloader",
    "hpu_torch",
    "optimum_habana",
})


# Graphcore IPU.
IPU_REQUIRED: FrozenSet[str] = frozenset({
    "poptorch",
    "popart",
    "poplar",
    "poplar_triton",
})


# AWS Inferentia / Trainium share the Neuron SDK; libraries that are
# explicitly compiled-for-Neuron go here. Generic torch is NOT here.
INFERENTIA_REQUIRED: FrozenSet[str] = frozenset({
    "transformers_neuronx",
    "neuronx_distributed",
    "torch_neuronx",
    "libneuronxla",
    "neuronx_cc",
})


# Trainium uses the same neuronx stack mostly; this set is for
# Trainium-specific training libraries that don't make sense on
# Inferentia.
TRAINIUM_REQUIRED: FrozenSet[str] = frozenset({
    "aws_neuron_trn",
})


# Apple Silicon (M1/M2/M3) — MLX is Apple's native ML framework that
# only runs on Apple Silicon. PyTorch's MPS backend doesn't have a
# distinct package (`import torch.backends.mps`), so it's not here.
MPS_REQUIRED: FrozenSet[str] = frozenset({
    "mlx",
    "mlx_lm",
    "mlx_data",
    "mlx_metal",
})


# Master map: accelerator type → set of libraries that require it.
# Order matters for ambiguous cases (rare): first hit wins.
ACCELERATOR_LIBRARIES: dict[str, FrozenSet[str]] = {
    "CUDA": CUDA_REQUIRED,
    "ROCM": ROCM_REQUIRED,
    "INTEL": INTEL_REQUIRED,
    "NPU": NPU_REQUIRED,
    "TPU": TPU_REQUIRED,
    "HABANA": HABANA_REQUIRED,
    "IPU": IPU_REQUIRED,
    "INFERENTIA": INFERENTIA_REQUIRED,
    "TRAINIUM": TRAINIUM_REQUIRED,
    "MPS": MPS_REQUIRED,
}


# Multi-target libraries — work on multiple accelerators depending on
# configuration. The profiler picks a deterministic default so the
# wizard's analyze step has SOMETHING to pre-fill; variants for the
# other accelerators get built as part of the variant build pipeline
# (so the user can still run on any target the platform supports).
#
# Default rationale per entry:
#   jax            CUDA   — most common GPU use; CPU/TPU/ROCM also work
#   openvino       INTEL  — it's an Intel framework, intended primarily for Intel hardware
#   onnxruntime    CUDA   — most common GPU-accelerated path
#   tensorflow     CUDA   — most common GPU-accelerated path
#   tf             CUDA   — alias
#   keras          CUDA   — most commonly used over TF/CUDA today
#   torch          CUDA   — note: pure-CPU torch is common; pick CUDA so we don't
#                           silently miss CUDA users, variant builds cover CPU
MULTI_TARGET_DEFAULTS: dict[str, str] = {
    "jax": "CUDA",
    "jaxlib": "CUDA",
    "openvino": "INTEL",
    "openvino_dev": "INTEL",
    "onnxruntime": "CUDA",
    "tensorflow": "CUDA",
    "tf": "CUDA",
    "keras": "CUDA",
    "torch": "CUDA",
    "pytorch": "CUDA",
}


# Common Python module-resolution pitfalls. Importers might write
# ``import gaussian_splatting`` even if the PyPI name uses hyphens.
# We normalise underscores and hyphens for matching.
_NORMALISE_RE = re.compile(r"[-_]+")


def _normalise(name: str) -> str:
    return _NORMALISE_RE.sub("_", name.strip().lower())


_CUDA_REQUIRED_NORM = frozenset(_normalise(n) for n in CUDA_REQUIRED)
_ROCM_REQUIRED_NORM = frozenset(_normalise(n) for n in ROCM_REQUIRED)


# Regex matching the top-level package in Python import statements.
# Catches:
#   - ``import nerfstudio``
#   - ``import nerfstudio.data.foo``
#   - ``from nerfstudio import data``
#   - ``from nerfstudio.data import foo``
# Misses (intentional — too noisy):
#   - dynamic imports (``__import__('nerfstudio')``)
#   - string references in comments
_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+([a-zA-Z_][\w]*)",
    re.MULTILINE,
)

# Some Dockerfile / requirements.txt mentions also reliably indicate
# the package will be installed. Match these for repo-mode analysis
# where the script under review may not be the only source of truth.
# Two patterns, used together by extract_package_candidates / scan_script:
#   1. Explicit `pip install X` / `RUN pip install X` — unambiguous.
#   2. Bare requirements.txt-shaped line — must be anchored at line
#      start AND followed by a version specifier OR end-of-line OR
#      comment OR extras bracket. Without that anchoring, the regex
#      false-fires on Python code (e.g. `args = parser.parse_args()`
#      would capture `args`) and burns PyPI requests on non-packages.
_PIP_INSTALL_VERB_RE = re.compile(
    r"(?:pip|RUN\s+pip)\s+install(?:\s+[-]{1,2}\S+)*\s+([a-zA-Z][\w.\-]*)",
    re.MULTILINE,
)
_REQUIREMENTS_BARE_RE = re.compile(
    # Anchored bare requirements.txt-style line. Terminators in
    # priority order:
    #   PEP 508 version specifier (==, >=, <=, !=, ~=, >, <)
    #   environment-marker semicolon
    #   trailing comment
    #   extras bracket with a letter inside (`torch[cuda]`) — the
    #     letter requirement distinguishes from `CMD [...]` /
    #     `if [ -n ... ]` Dockerfile + shell tokens that previously
    #     false-matched and polluted the library candidate list.
    #   end-of-line (a bare package name on its own line)
    r"^([a-zA-Z][\w.\-]*)\s*(?:==|>=|<=|!=|~=|>|<|;|#|\[[a-zA-Z]|$)",
    re.MULTILINE,
)
# Kept as an alias for the few callers that still reference it
# (scan_script's existing API). New callers use the two specific
# regexes above for context-appropriate matching.
_PIP_INSTALL_RE = _PIP_INSTALL_VERB_RE


@dataclass
class GpuOverride:
    """The result of scanning a script. ``required_process_type`` is
    the override the caller should apply; ``triggering_imports`` is
    the list of detected library names — surface to the user so they
    know WHY we overrode."""
    required_process_type: str   # "CUDA" or "ROCM"
    triggering_imports: Tuple[str, ...]

    def reason(self) -> str:
        """One-sentence explanation suitable for the wizard chat /
        clarifying_questions panel."""
        joined = ", ".join(self.triggering_imports)
        return (
            f"Detected {joined} in the script — these libraries require a "
            f"{self.required_process_type} GPU at runtime. Switched processing "
            f"from CPU to {self.required_process_type}. If this is wrong, "
            f"change it back in the Review step."
        )


def extract_package_candidates(text: str) -> list[str]:
    """Return the de-duplicated list of package names that appear as
    Python imports OR pip-install commands OR requirements.txt-style
    lines in ``text``. Used by both the GPU override path (analyze
    step) and the variant restriction path (build pipeline) to find
    what libraries a script declares.

    Three patterns combined:
      1. ``import X`` / ``from X import ...`` (Python source)
      2. ``pip install X`` / ``RUN pip install X`` (Dockerfile, scripts)
      3. Bare ``X==1.0`` / ``X>=1.0`` / ``X`` (requirements.txt lines)

    The third pattern requires a PEP 508 version specifier OR
    end-of-line OR comment OR extras bracket — without that anchoring
    it would false-fire on ordinary Python lines (`args = ...` → `args`)
    and waste PyPI lookups on non-packages.

    Returns RAW package names (un-normalised) so callers can surface
    them in user-facing messages.
    """
    if not text:
        return []
    seen: set[str] = set()
    candidates: list[str] = []
    for pattern in (_IMPORT_RE, _PIP_INSTALL_VERB_RE, _REQUIREMENTS_BARE_RE):
        for m in pattern.finditer(text):
            raw = m.group(1)
            norm = _normalise(raw)
            if norm and norm not in seen:
                seen.add(norm)
                candidates.append(raw)
    return candidates


# Binaries the wizard's scripts commonly shell out to whose typical use
# is GPU-accelerated. Import-sniffing misses these completely — they are
# invoked via subprocess.run(["NAME", ...]) or os.system("NAME ...") and
# carry no Python import trail. Conservative bias: only list tools whose
# heavy work is GPU in normal usage (everyone runs COLMAP's dense_stereo
# with --gpu, not CPU).
#
# Adding to this map fires the same override as importing one of the
# CUDA_REQUIRED Python libraries: when the analyzer LLM declared
# process_type=CPU, the wizard upgrades to CUDA and surfaces the tool
# name in the triggering_imports tuple.
_GPU_BINARY_TOOLS: dict[str, str] = {
    # Top-level CLI tools that ship one binary with subcommands.
    "colmap":      "CUDA",  # Structure-from-Motion + CUDA dense MVS
    "openmvs":     "CUDA",  # legacy single-binary form
    "openmvg":     "CUDA",  # legacy single-binary form
    "alicevision": "CUDA",  # legacy single-binary form
    "meshroom":    "CUDA",  # AliceVision GUI batch CLI
    "glomap":      "CUDA",  # Global SfM, GPU dense
    "ns-train":    "CUDA",  # nerfstudio training CLI
    "ns-process-data": "CUDA",  # nerfstudio data prep (calls colmap)
    # OpenMVS publishes ONE per-stage binary (no shared prefix). Each
    # of these is GPU-accelerated in normal use — DensifyPointCloud is
    # the canonical CUDA-heavy step.
    "densifypointcloud": "CUDA",
    "reconstructmesh":   "CUDA",
    "refinemesh":        "CUDA",
    "texturemesh":       "CUDA",
    "interfacecolmap":   "CUDA",
}

# Per-family prefix conventions. openMVG and AliceVision each split
# their pipelines across many per-stage binaries with a shared prefix
# (openMVG_main_SfMInit_ImageListing, aliceVision_cameraInit, …) — those
# are the names actually invoked in real scripts, NOT a single "openmvg"
# or "alicevision" command. Matching the prefix + word characters
# captures every per-stage binary without listing each by name.
_GPU_BINARY_PREFIXES: dict[str, str] = {
    "openMVG_main_": "CUDA",  # openMVG_main_SfMInit_ImageListing, _ComputeFeatures, _IncrementalSfM, …
    "aliceVision_":  "CUDA",  # aliceVision_cameraInit, _featureExtraction, _meshFiltering, …
    "OpenMVS_":      "CUDA",  # older OpenMVS naming convention
}

# Combined regex: exact tool names OR prefix+wordchars. Anchored on
# quotes (matches the subprocess pattern `["colmap", ...]` or
# `"colmap ..."`) so identifier-substring matches like
# `from colmap_helpers import …` don't false-fire.
_BINARY_REFERENCE_RE = re.compile(
    r"""['"](?P<tool>"""
    + "|".join(re.escape(t) for t in _GPU_BINARY_TOOLS)
    + "|"
    + "|".join(re.escape(p) + r"\w+" for p in _GPU_BINARY_PREFIXES)
    + r""")(?=['"]|\s)""",
    re.IGNORECASE,
)


def extract_gpu_binary_shellouts(text: str) -> list[tuple[str, str]]:
    """Detect references to GPU-accelerated CLI binaries the script
    shells out to (colmap, openmvs, alicevision, openMVG_main_*,
    DensifyPointCloud, …). Returns a deduplicated list of
    ``(binary_name, accelerator)`` tuples.

    This catches the photogrammetry / 3D-vision case where the heavy
    lifting is in a binary rather than a Python library — import-sniffing
    sees nothing and the analyzer LLM defaults to CPU.

    Deduplication is per FAMILY (tool name or prefix), not per binary
    name — a script that runs every openMVG_main_* stage reports the
    family once rather than 10x.
    """
    if not text:
        return []
    seen_keys: set[str] = set()
    hits: list[tuple[str, str]] = []
    for m in _BINARY_REFERENCE_RE.finditer(text):
        match_str = m.group("tool")
        match_lower = match_str.lower()
        # Exact-name match against the tool map (case-insensitive
        # because the regex was IGNORECASE).
        if match_lower in _GPU_BINARY_TOOLS:
            if match_lower in seen_keys:
                continue
            seen_keys.add(match_lower)
            hits.append((match_lower, _GPU_BINARY_TOOLS[match_lower]))
            continue
        # Otherwise the match was via a family prefix — find which.
        for prefix, accel in _GPU_BINARY_PREFIXES.items():
            if match_lower.startswith(prefix.lower()):
                key = prefix.lower()
                if key in seen_keys:
                    break
                seen_keys.add(key)
                hits.append((match_str, accel))
                break
    return hits


def is_multi_target(package: str) -> bool:
    """True when ``package`` is one of the multi-target libraries
    that work on multiple accelerators (jax, openvino, torch, etc.).
    The variant restriction layer treats multi-target libs as
    no-restriction even though the profiler returns a deterministic
    default — the default is a wizard pre-fill, not a hard
    requirement, so variants for every accelerator stay compatible.
    """
    return _normalise(package) in {_normalise(n) for n in MULTI_TARGET_DEFAULTS.keys()}


def scan_script(script: str) -> Optional[GpuOverride]:
    """Scan a script (or Dockerfile / requirements.txt content) for
    GPU-only library references. Returns a ``GpuOverride`` if any are
    found, else ``None``.

    Order of preference when both CUDA and ROCm libs appear: CUDA
    wins. In practice the two are mutually exclusive in real code
    (ROCm builds use rocm-specific package names) so this is mostly
    theoretical.
    """
    if not script:
        return None

    found_cuda: list[str] = []
    found_rocm: list[str] = []
    seen: set[str] = set()

    # Pass 1: Python import statements.
    for m in _IMPORT_RE.finditer(script):
        raw = m.group(1)
        norm = _normalise(raw)
        if norm in seen:
            continue
        seen.add(norm)
        if norm in _CUDA_REQUIRED_NORM:
            found_cuda.append(raw)
        elif norm in _ROCM_REQUIRED_NORM:
            found_rocm.append(raw)

    # Pass 2: pip install verbs AND bare requirements.txt lines.
    for pattern in (_PIP_INSTALL_VERB_RE, _REQUIREMENTS_BARE_RE):
        for m in pattern.finditer(script):
            raw = m.group(1)
            norm = _normalise(raw)
            if norm in seen:
                continue
            seen.add(norm)
            if norm in _CUDA_REQUIRED_NORM:
                found_cuda.append(raw)
            elif norm in _ROCM_REQUIRED_NORM:
                found_rocm.append(raw)

    if found_cuda:
        return GpuOverride(
            required_process_type="CUDA",
            triggering_imports=tuple(found_cuda),
        )
    if found_rocm:
        return GpuOverride(
            required_process_type="ROCM",
            triggering_imports=tuple(found_rocm),
        )
    return None


def _script_has_gpu_signal(script: str) -> bool:
    """True if the script actually references a GPU library (per the profiler)
    or a GPU CLI tool. Used to detect a HALLUCINATED GPU process_type: when the
    analyzer LLM claims CUDA/ROCM/etc. on a script with no GPU signal at all it
    is wrong (repro 2026-09-01: pypdfium2 / PDFium — a CPU PDF renderer —
    mislabelled 'CUDA-accelerated'). Deterministic binary scan first, then the
    per-import profiler; short-circuits on the first GPU hit."""
    if not script:
        return False
    if extract_gpu_binary_shellouts(script):
        return True
    try:
        from services.library_profiler import profile as _profile_lib
    except Exception:  # noqa: BLE001 — profiler unavailable -> no opinion
        return False
    for raw in extract_package_candidates(script):
        try:
            result = _profile_lib(raw)
        except Exception:  # noqa: BLE001
            continue
        if result and result not in ("CPU", "UNKNOWN"):
            return True
    return False


def _reset_gpu_base_to_cpu(analyzed: dict) -> None:
    """If a downgrade landed process_type on CPU, drop a GPU suggested_base_image
    back to the CPU default so the two stay consistent."""
    sbi = (analyzed.get("suggested_base_image") or "").lower()
    if sbi.startswith(("cuda", "rocm", "intel", "npu")):
        analyzed["suggested_base_image"] = "python-3.11"


_ACCEL_CONFIRM_THRESHOLD = float(os.getenv("WIZARD_ACCEL_CONFIRM_THRESHOLD", "0.7"))

_ACCEL_CONFIRM_SYSTEM_PROMPT = """You are double-checking an accelerator \
classification before a container is built. A script was auto-classified as \
needing a GPU/accelerator. Re-read the ACTUAL code and answer honestly: does it \
REALLY need one?

A task needs a GPU/accelerator ONLY if it runs tensor / neural-network / CUDA \
compute — e.g. torch, tensorflow, jax, cupy, onnxruntime-gpu, or a \
GPU-accelerated CLI/binary (ffmpeg with nvenc, COLMAP dense reconstruction).

The following are CPU tasks even though they handle images/media/documents: \
PDF parsing or page rasterisation (pypdfium2, PyMuPDF, poppler), image \
resize/convert/crop (Pillow, pypdfium2, OpenCV on CPU), plain file I/O, HTTP / \
scraping, tabular data processing (pandas, numpy without cupy), plain ffmpeg \
transcodes. Rendering a PDF page to a PNG is CPU work.

A deterministic scan of the imports and shell-outs found NO GPU library or GPU \
CLI tool. So unless the code clearly runs GPU compute you missed, the honest \
answer is that it does NOT need a GPU.

Return ONLY JSON, no prose:
{"needs_accelerator": true|false,
 "process_type": "CPU"|"CUDA"|"ROCM"|"INTEL"|"NPU",
 "confidence": 0.0-1.0,
 "reason": "one sentence"}
Only answer needs_accelerator=true if you can point to specific GPU-compute code."""


def confirm_accelerator_need(
    analyzed: dict,
    script: str,
    *,
    llm=None,
) -> dict:
    """Second-guess a GPU classification: 'are you sure this needs a GPU?'.

    Order of authority (GPU-need is a FACTUAL question, so the deterministic
    scan leads; the LLM is only a backstop for the ambiguous case):
      1. process_type is CPU  -> nothing to second-guess (apply_override handles
         the CPU->GPU upgrade separately).
      2. A real GPU library / GPU CLI tool IS present -> trust the GPU call.
      3. GPU claimed but NO GPU signal -> suspicious. Ask the model to re-read
         the code ('are you sure?'); downgrade to CPU when it agrees (or when no
         LLM is available — no GPU signal means CPU by default). This is the
         pypdfium2/PDFium 'CUDA-accelerated' hallucination (2026-09-01): left
         alone it triggers a ~30min GPU build per accelerator variant for a CPU
         task.

    Mutates + returns ``analyzed``. Never raises.
    """
    current = (analyzed.get("process_type") or "CPU").upper()
    if current == "CPU" or not script:
        return analyzed
    if _script_has_gpu_signal(script):
        return analyzed  # a genuine GPU library/binary — trust it

    if llm is None:
        try:
            from utils.llm_client import call_wizard_llm

            def llm(*, system_prompt, user_prompt):  # type: ignore[misc]
                return call_wizard_llm(
                    system_prompt=system_prompt, user_prompt=user_prompt,
                    response_format="json", timeout_s=45,
                )
        except Exception:  # noqa: BLE001
            llm = None

    if llm is None:
        # No LLM — the deterministic fact stands: no GPU signal => CPU.
        logger.info(
            "accel-confirm: no LLM; downgrading %s -> CPU (no GPU signal)", current,
        )
        analyzed["process_type"] = "CPU"
        _reset_gpu_base_to_cpu(analyzed)
        return analyzed

    user_prompt = (
        f"AUTO-CLASSIFIED AS: {current}\n"
        f"STATIC SCAN: no GPU library or GPU CLI tool found in the imports or "
        f"shell-outs.\n\nSCRIPT:\n```python\n{script.strip()[:6000]}\n```"
    )
    try:
        verdict = llm(
            system_prompt=_ACCEL_CONFIRM_SYSTEM_PROMPT, user_prompt=user_prompt,
        )
    except Exception as e:  # noqa: BLE001 — fall back to the deterministic call
        logger.info("accel-confirm: LLM unavailable (%s); downgrading to CPU", e)
        analyzed["process_type"] = "CPU"
        _reset_gpu_base_to_cpu(analyzed)
        return analyzed

    if isinstance(verdict, dict) and verdict.get("needs_accelerator") is False:
        try:
            conf = float(verdict.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf >= _ACCEL_CONFIRM_THRESHOLD:
            logger.info(
                "accel-confirm: downgraded %s -> CPU (%s)",
                current, str(verdict.get("reason") or "")[:120],
            )
            analyzed["process_type"] = "CPU"
            _reset_gpu_base_to_cpu(analyzed)
    return analyzed


def apply_override(
    analyzed: dict,
    script: str,
) -> dict:
    """Post-process an ``analyze_script`` result, overriding
    ``process_type`` when a known-GPU library is imported.

    Mutates and returns the dict. If the LLM already chose CUDA/ROCM
    (or something else GPU-y), we trust it and leave alone — the
    override only fires when the LLM said CPU.

    Per-import GPU requirement is resolved through
    ``services.library_profiler.profile()`` which combines:
      - cached learnings from prior calls (Postgres ``library_profiles``)
      - the curated seed list below
      - PyPI metadata (classifier + recursive deps)
    The curated set is the fast/offline path; the profiler is the
    learning path that catches libraries we haven't seen by hand.
    """
    current = (analyzed.get("process_type") or "CPU").upper()
    if current != "CPU":
        return analyzed

    # Find all top-level imports + pip-install / requirements lines, AND
    # any GPU-CLI binary shell-outs (colmap / openmvs / alicevision / …).
    # The shell-out path catches photogrammetry-style scripts where the
    # heavy lifting is a binary, not a Python library.
    if not script:
        return analyzed
    candidates = extract_package_candidates(script)
    binary_hits = extract_gpu_binary_shellouts(script)
    if not candidates and not binary_hits:
        return analyzed

    # Profile each import — short-circuit on the first accelerator hit
    # (any non-CPU, non-UNKNOWN result). Profiler consults its cache
    # + curated seed first, only falls through to PyPI when we
    # genuinely don't know the lib.
    from services.library_profiler import profile as _profile_lib  # local import to avoid startup cycle
    triggering: list[tuple[str, str]] = []  # (library, accelerator_type)
    for raw in candidates:
        result = _profile_lib(raw)
        if result and result not in ("CPU", "UNKNOWN"):
            triggering.append((raw, result))
            # Stop on the first DEPLOYABLE accelerator — that's the one
            # we'll use. For non-deployable, keep scanning so we report
            # the lib if a deployable one exists later in the list.
            if result in DEPLOYABLE_TYPES:
                break

    # Fold in binary shell-out hits when no deployable import hit was
    # found. Each entry already names its accelerator (CUDA per the
    # _GPU_BINARY_TOOLS map), so no profiler lookup is needed.
    if not any(t[1] in DEPLOYABLE_TYPES for t in triggering):
        triggering.extend(binary_hits)

    if not triggering:
        return analyzed

    # Prefer the FIRST deployable hit; if all hits are non-deployable,
    # surface the first one so the user sees the most useful clarifying
    # message ("you imported X which needs TPU; we don't deploy TPU yet").
    deployable_hit = next(((lib, t) for lib, t in triggering if t in DEPLOYABLE_TYPES), None)
    chosen_lib, chosen_type = deployable_hit or triggering[0]

    override = GpuOverride(
        required_process_type=chosen_type,
        triggering_imports=tuple(lib for lib, _ in triggering),
    )

    if chosen_type in DEPLOYABLE_TYPES:
        logger.info(
            "gpu_library_index: overriding process_type CPU -> %s based on imports: %s",
            chosen_type,
            ", ".join(override.triggering_imports),
        )
        analyzed["process_type"] = chosen_type
    else:
        # Non-deployable accelerator detected — leave process_type at
        # CPU (the safest fallback) but warn the user prominently in
        # clarifying_questions. Their build will likely fail at
        # smoke-test time; better they know up front so they can
        # bring their own base image or refactor.
        logger.warning(
            "gpu_library_index: detected non-deployable accelerator %s from %s — "
            "leaving process_type=CPU; surfacing clarifying question",
            chosen_type,
            chosen_lib,
        )

    # Bump suggested_base_image to match if it's still the CPU default
    # — only for deployable types. Non-deployable types stay on CPU
    # base since we have no other image to point at.
    _BASE_BY_TYPE = {
        "CUDA": "cuda-12.4-python3.11",
        "ROCM": "rocm-6.1-python3.11",
        "INTEL": "intel-xpu-2025.0",
        "NPU": "npu-openvino-2025",
    }
    if chosen_type in _BASE_BY_TYPE:
        sbi = (analyzed.get("suggested_base_image") or "").lower()
        if not sbi or sbi.startswith("python-") or sbi.startswith("node-") or sbi.startswith("alpine"):
            analyzed["suggested_base_image"] = _BASE_BY_TYPE[chosen_type]

    # Surface the override to the user as a clarifying question so
    # it isn't a silent change. Phrasing differs for deployable vs.
    # non-deployable types: deployable explains the auto-promotion,
    # non-deployable explains the gap.
    qs = analyzed.get("clarifying_questions") or []
    if not isinstance(qs, list):
        qs = []

    if chosen_type in DEPLOYABLE_TYPES:
        question_text = override.reason()
        suggested = chosen_type
    else:
        triggering_str = ", ".join(override.triggering_imports)
        question_text = (
            f"Detected {triggering_str} in the script — these libraries require a {chosen_type} "
            f"accelerator at runtime. We don't currently deploy {chosen_type} (no base image in "
            f"the platform yet). Build will likely fail at smoke-test time unless you bring your "
            f"own base image. Leaving processing on CPU as the fallback."
        )
        suggested = "CPU"

    qs.insert(0, {
        "id": "gpu_library_override",
        "question": question_text,
        "field": "process_type",
        "suggested_answer": suggested,
        "detected_accelerator": chosen_type,
        "deployable": chosen_type in DEPLOYABLE_TYPES,
    })
    analyzed["clarifying_questions"] = qs

    return analyzed
