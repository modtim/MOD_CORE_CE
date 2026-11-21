"""Static analyzer that decides which backends a source can NOT auto-port to.

The classifier scans the input source files for known patterns and emits
one feature tag per pattern. Each tag is then cross-referenced against
the capability matrix:

    if SUPPORT[backend][tag] == "blocked":
        emit incompatibility for that backend with the tag's user-facing
        reason.
    if SUPPORT[backend][tag] == "rewrite" and the recipe library doesn't
        have an entry for that tag yet (Phase 1):
        emit "rewrite recipe not yet shipped" with the same reason.

The classifier is **pure regex/AST** — no LLM, no hardware. It runs on
every wizard build and is the only layer needed to flip the wizard's
"pending forever" state into "incompatible — <specific reason>".

Tag detectors are intentionally narrow: false negatives are fine
(Phase 2 recipes pick up patterns the classifier missed), but false
positives ship broken images. When in doubt, leave the pattern out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Set, Tuple

from .capability_matrix import (
    BACKENDS, SUPPORT, is_blocked, needs_rewrite,
)


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------
#
# Each rule maps a regex (applied to the joined source text) to one of the
# capability_matrix.FEATURES tags + a one-line, user-facing description.
# The description is what shows up in the wizard's per-variant tile when
# the rule fires for a backend that's "blocked" or unimplemented "rewrite"
# on that tag.
#
# Patterns must be specific enough that they don't fire on stray
# substrings inside string literals or comments. They aren't perfect; the
# trade-off is preferring false negatives (fail open — miss a port
# blocker) over false positives (refuse a perfectly-portable source).
#
# Imports use word boundaries + module-name boundaries so e.g. matching
# "from triton" won't fire on "from tritonserver" or "from my.triton.utils".

@dataclass(frozen=True)
class _Rule:
    feature: str
    pattern: re.Pattern
    reason: str

    def __init__(self, feature: str, pattern: str, reason: str):
        # frozen dataclass + non-default __init__ requires object.__setattr__
        object.__setattr__(self, "feature", feature)
        object.__setattr__(self, "pattern", re.compile(pattern, re.MULTILINE))
        object.__setattr__(self, "reason", reason)


_RULES: Tuple[_Rule, ...] = (
    # --- explicit cuda-namespace API calls ---
    _Rule(
        "cuda_namespace_explicit",
        r"\btorch\.cuda\.(synchronize|amp\.autocast|amp\.GradScaler|"
        r"is_available|device_count|current_device|set_device|"
        r"empty_cache|memory_allocated|memory_reserved|"
        r"max_memory_allocated|reset_peak_memory_stats|"
        r"Stream|Event|graphs)\b",
        "uses torch.cuda.* explicit API calls — these need a "
        "namespace rewrite (torch.xpu.*) for Intel; no equivalent on "
        "NPU.",
    ),
    # --- custom CUDA kernels / extensions ---
    # Tightened: only the patterns that ACTUALLY compile C++/CUDA at
    # build or runtime should fire. Earlier the rule also matched
    # `torch.utils.cpp_extension` (just the module import — most repos
    # have it without using it) and `@torch.jit.script` /
    # `@torch.jit.trace` (pure Python JIT — no C++ compile, runs
    # everywhere). Both produced false positives that mis-blocked
    # ordinary inference repos for Intel/NPU. The remaining patterns
    # are the unambiguous compile-time signals:
    #   • `CUDAExtension(...)` / `CppExtension(...)` — setup.py uses
    #     these to declare a build-time native extension.
    #   • `cpp_extension.load(...)` — runtime JIT compile of C++/CUDA.
    # Repos that ship `.cu` / `.cuh` / `.dp.cpp` files still get
    # caught by the source-filename heuristic in detect_features.
    _Rule(
        "custom_cuda_kernels",
        r"\b(CUDAExtension|CppExtension|cpp_extension\.load)\b",
        "compiles custom CUDA/C++ extensions — no auto-translation to "
        "SYCL/DPC++ exists; manual port required for Intel/NPU.",
    ),
    # --- flash_attn ---
    _Rule(
        "flash_attn",
        r"^\s*(from|import)\s+flash_attn\b",
        "imports flash_attn — implementation is CUDA-only (a flash_attn-"
        "rocm fork exists for ROCm but not auto-applied here; no Intel/"
        "NPU equivalent shipped today).",
    ),
    # --- triton kernels ---
    _Rule(
        "triton_kernels",
        r"^\s*(from|import)\s+triton(\.|\s|$)",
        "imports triton — kernels are typically CUDA-targeted; "
        "triton-xpu is in beta and not all kernels port.",
    ),
    # --- NCCL distributed ---
    _Rule(
        "nccl_distributed",
        r"\b(backend\s*=\s*[\"']nccl[\"']|"
        r"torch\.distributed\.init_process_group\([^)]*nccl)",
        "uses NCCL backend — needs RCCL on ROCm / oneCCL on Intel; no "
        "distributed-training story on NPU.",
    ),
    # --- pin_memory ---
    _Rule(
        "pin_memory_dataloader",
        r"\bpin_memory\s*=\s*True\b",
        "uses pin_memory=True — CUDA-specific perf path; harmless to "
        "leave on for ROCm but a no-op (sometimes warning) on CPU/Intel/"
        "NPU.",
    ),
    # --- mixed precision via torch.cuda.amp explicitly ---
    _Rule(
        "mixed_precision_explicit",
        r"\btorch\.cuda\.amp\.(autocast|GradScaler|custom_fwd|"
        r"custom_bwd)\b",
        "explicit torch.cuda.amp — needs torch.xpu.amp on Intel; CPU's "
        "autocast is limited; NPUs use static quantization instead of "
        "runtime amp.",
    ),
    # --- FP8 dtype ---
    _Rule(
        "fp8_dtype",
        r"\btorch\.float8_e(4m3fn|5m2)\b",
        "uses FP8 dtypes — H100/MI300/PVC only; no portable equivalent.",
    ),
    # --- torch.compile (cuda-targeted) ---
    _Rule(
        "torch_compile_cuda",
        r"\btorch\.compile\([^)]*mode\s*=\s*[\"'](reduce-overhead|"
        r"max-autotune)[\"']",
        "torch.compile mode targets CUDA-specific autotuning; default "
        "mode is portable, but reduce-overhead/max-autotune currently "
        "skip on non-CUDA.",
    ),
    # --- HF transformers inference (the common shape we DO support) ---
    _Rule(
        "transformers_inference",
        r"\bfrom\s+transformers\s+import\b",
        "uses HuggingFace transformers — supported on Intel via IPEX "
        "rewrite, supported on NPU only for architectures in the "
        "optimum-intel allowlist.",
    ),
    # --- vLLM ---
    _Rule(
        "vllm_server",
        r"\b(from|import)\s+vllm\b",
        "uses vLLM — has separate forks (vllm-rocm, vllm-xpu); NPU "
        "deployment doesn't exist today.",
    ),
    # --- DeepSpeed ---
    _Rule(
        "deepspeed",
        r"\b(from|import)\s+deepspeed\b",
        "uses DeepSpeed — has Intel XPU plugin; not viable on NPU "
        "(training-focused).",
    ),
    # --- FSDP ---
    _Rule(
        "fsdp",
        r"\bFullyShardedDataParallel\b|"
        r"\btorch\.distributed\.fsdp\b",
        "uses FSDP — supported on ROCm, supported on Intel XPU via "
        "ipex.distributed; not applicable on NPU.",
    ),
    # --- quantization libs ---
    _Rule(
        "quantization_int8",
        r"\b(from|import)\s+(bitsandbytes|auto_gptq|awq|"
        r"optimum\.gptq)\b",
        "uses INT8 quantization library — bitsandbytes is CUDA/ROCm; "
        "Intel uses intel-extension-for-transformers; NPU uses "
        "OpenVINO POT.",
    ),
    # --- openvino / optimum-intel ---
    _Rule(
        "openvino_runtime",
        r"\b(from|import)\s+(openvino|optimum\.intel)\b",
        "imports openvino/optimum-intel — native NPU/CPU; no CUDA path.",
    ),
    # --- IPEX ---
    _Rule(
        "ipex_runtime",
        r"\b(from|import)\s+intel_extension_for_pytorch\b",
        "imports intel_extension_for_pytorch — native Intel XPU; "
        ".to('xpu') in source signals an Intel-targeted build.",
    ),
)


# Pattern source-file extensions that signal compiled CUDA code present
# in the repo (i.e. the source file alone might not match, but a sibling
# .cu file means custom kernels exist).
_CUDA_SOURCE_EXTENSIONS = (".cu", ".cuh", ".dp.cpp")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class Incompatibility:
    """A single (backend, reason) pair the classifier emitted. The wizard
    surfaces this as the variant tile's incompatible-state explanation."""
    backend: str
    feature: str
    reason: str

    def to_dict(self) -> Dict[str, str]:
        return {"backend": self.backend, "feature": self.feature,
                "reason": self.reason}


def detect_features(source_text: str, source_filenames: Iterable[str] = ()) -> Set[str]:
    """Return the set of feature tags fired by the source.

    `source_text` is the concatenated body of every input file. Comments
    and string literals are NOT stripped — they're rare false-positive
    sources for these patterns and stripping them properly costs more
    than it saves.

    `source_filenames` lets us catch repos that ship `.cu` / `.cuh` /
    `.dp.cpp` files alongside Python — a strong signal of compiled
    custom kernels even when the .py doesn't match a regex.
    """
    fired: Set[str] = set()
    for rule in _RULES:
        if rule.pattern.search(source_text):
            fired.add(rule.feature)

    # Filename heuristic — a sibling `.cu` file beats every regex.
    for fn in source_filenames:
        lower = fn.lower()
        if any(lower.endswith(ext) for ext in _CUDA_SOURCE_EXTENSIONS):
            fired.add("custom_cuda_kernels")
            break

    return fired


def classify(
    source_text: str,
    source_filenames: Iterable[str] = (),
    *,
    targets: Iterable[str] = BACKENDS,
    implemented_recipes: Iterable[str] = (),
) -> Dict[str, Incompatibility]:
    """Determine which target backends the source CANNOT auto-port to.

    Returns a {backend: Incompatibility} dict. Backends absent from the
    returned dict are auto-port-clean (modulo whatever the recipe
    library does with the source).

    `implemented_recipes` is the set of backends with a real Phase-2+
    recipe wired up. Backends NOT in this set treat any "rewrite"
    requirement as "blocked for now" — that's how Phase 1 surfaces
    "Intel XPU recipe not yet shipped" without pretending we have it.
    """
    fired = detect_features(source_text, source_filenames)
    if not fired:
        return {}

    implemented = set(implemented_recipes)
    out: Dict[str, Incompatibility] = {}

    # For each (backend, feature) pair: pick the FIRST blocker we hit.
    # Order matters because we want the most-actionable message wins —
    # a "blocked" feature is more informative than "rewrite".
    rule_by_feature = {r.feature: r for r in _RULES}

    for backend in targets:
        if backend not in SUPPORT:
            continue
        # Walk fired features in matrix-declaration order so the
        # message is deterministic across runs.
        chosen: Incompatibility | None = None
        for feature in fired:
            rule = rule_by_feature.get(feature)
            if rule is None:
                continue
            level = SUPPORT[backend].get(feature, "blocked")
            if level == "blocked":
                chosen = Incompatibility(backend, feature, rule.reason)
                break
            if level == "rewrite" and backend not in implemented:
                # Phase-1 framing: "rewrite required + recipe not shipped"
                chosen = Incompatibility(
                    backend, feature,
                    rule.reason + " (auto-port recipe not yet shipped "
                    "— see MULTIVENDOR_ROADMAP.md Phase 2/3).",
                )
                # Don't break — a "blocked" later in the loop should win
                # over a "rewrite" because blocked is the harder stop.
        if chosen is not None:
            out[backend] = chosen

    return out
