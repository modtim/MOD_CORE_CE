"""Per-variant source adaptation for the New Node wizard.

The wizard generates a hero source file (a Python .py, an R .R, a
Julia .jl, a C++ .cu, a shell script, …) written for NVIDIA CUDA or
the most common target. Historically we built N GPU variants (rocm,
intel, vulkan, npu, cpu, metal, …) by swapping the Dockerfile's FROM
line and the torch --index-url — nothing else. The user's source was
copied byte-identically into every image.

That's a lie: a script doing `model.to("cuda")` builds cleanly on a
ROCm base (torch wheels exist) but crashes at runtime because AMD's
ROCm-torch and NVIDIA's CUDA-torch use different device-selection
idioms. Intel XPU needs `.to("xpu")`. Julia uses `CUDA.jl` vs
`AMDGPU.jl` — entirely different packages. R's torch package only has
CPU + CUDA. NPU workloads need OpenVINO and can't host plain torch.
Variants were scheduleable but silent-fail-prone.

This module produces a rewritten source PER (variant, language). The
same LLM client the wizard already uses (tools.llm_providers) gets a
prompt tuned with per-(language, variant) guidance — so the adapter
knows, for example, that Julia-on-Intel means `oneAPI.jl` and
Julia-on-Metal means `Metal.jl`, while Python-on-Intel means
`intel_extension_for_pytorch`.

When the (language, variant) pair is fundamentally unsupported
(R-on-NPU, Julia-on-TPU, shell-on-Groq, etc.), we short-circuit with
`incompatible=true` — the caller drops that variant from the build.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from tools.llm_providers import LLMConfig, get_provider
except ImportError:
    # Allow standalone use (e.g. `python -m variant_adapter`) without
    # the full containerizer package on PYTHONPATH.
    from llm_providers import LLMConfig, get_provider  # type: ignore

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------
# Redis-backed cache for AdaptResult
# -----------------------------------------------------------------
#
# The adapter's work is deterministic per (hero_body, language,
# variant, filename, prompt_template_version): identical inputs →
# identical outputs. Caching the `AdaptResult` keeps tenants who
# rebuild the same wizard (minor edits, rerunning the same node
# generation) from paying the LLM round-trip latency again. At ~4s
# per call × 13 variants × N files, the savings are real.
#
# Two invalidation levers:
#   1. Global: bump PROMPT_TEMPLATE_VERSION when the prompt text or
#      VARIANT_GUIDANCE tables change — every cached entry rolls.
#   2. Per-tenant: a Dashboard admin action INCRs a tenant salt at
#      mod:adapter:tenant:{id}:salt. Next cache read/write uses the
#      new salt, so old entries are orphaned (and stale out via TTL).
#
# Graceful degradation: if REDIS_URL is unset or Redis is unreachable,
# the cache silently returns misses. Adapter runs as if no cache were
# configured.

PROMPT_TEMPLATE_VERSION = "v1"   # bump to roll the global cache.
_CACHE_TTL_SECONDS = 7 * 24 * 3600
_CACHE_NAMESPACE = "mod:adapter:v2"

_redis_client: Any = None
_redis_init_tried = False


def _get_redis() -> Any:
    """Lazy-initialise a Redis client from REDIS_URL. Returns None if
    Redis isn't available — callers treat None as "cache disabled"."""
    global _redis_client, _redis_init_tried
    if _redis_init_tried:
        return _redis_client
    _redis_init_tried = True
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        import redis  # local import so the module loads without redis installed
        _redis_client = redis.Redis.from_url(
            url, socket_timeout=1.5, socket_connect_timeout=1.5,
            decode_responses=True,
        )
        _redis_client.ping()
        logger.info("variant_adapter cache: connected to Redis")
        return _redis_client
    except Exception as e:  # noqa: BLE001 — redis-py raises a pile of subclasses
        logger.info("variant_adapter cache: disabled (%s)", e)
        _redis_client = None
        return None


def _tenant_salt(tenant_id: Optional[int]) -> str:
    """Return the tenant's current cache salt, or an empty string when
    the cache is disabled / tenant_id absent. INCR'ing this key is
    what the Dashboard "Clear adapter cache" button does — see the
    /platform/tenants/{id}/adapter-cache/flush endpoint."""
    if tenant_id is None:
        return ""
    client = _get_redis()
    if client is None:
        return ""
    try:
        salt = client.get(f"mod:adapter:tenant:{tenant_id}:salt") or "0"
        return str(salt)
    except Exception as e:  # noqa: BLE001
        logger.debug("variant_adapter salt fetch failed: %s", e)
        return ""


def _cache_key(
    *,
    tenant_id: Optional[int],
    language: str,
    variant: str,
    filename: str,
    body: str,
) -> str:
    """Build the Redis key for one (tenant × language × variant ×
    filename × body) tuple. Salt segment changes whenever the tenant
    runs a cache flush; template-version segment changes when we edit
    the prompt globally.
    """
    salt = _tenant_salt(tenant_id)
    digest_input = (
        f"{PROMPT_TEMPLATE_VERSION}|{language}|{variant}|{filename}|{body}"
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    # Tenant salt goes ahead of the digest so a salt bump orphans all
    # prior keys instantly — we don't need to DEL them; TTL handles it.
    return f"{_CACHE_NAMESPACE}:{salt}:{digest}"


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    client = _get_redis()
    if client is None:
        return None
    try:
        raw = client.get(key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        logger.debug("variant_adapter cache GET failed: %s", e)
        return None


def _cache_set(key: str, payload: Dict[str, Any]) -> None:
    client = _get_redis()
    if client is None:
        return
    try:
        client.setex(key, _CACHE_TTL_SECONDS, json.dumps(payload))
    except Exception as e:  # noqa: BLE001
        logger.debug("variant_adapter cache SET failed: %s", e)


# -----------------------------------------------------------------
# Language detection
# -----------------------------------------------------------------

# Ordered list: first match wins. Extension + shebang are the strong
# signals; content heuristics are only consulted if both are missing.
_SHEBANG_LANG = [
    (re.compile(r"^#!.*\bpython[0-9.]*\b"),    "python"),
    (re.compile(r"^#!.*\bRscript\b"),          "r"),
    (re.compile(r"^#!.*\bjulia\b"),            "julia"),
    (re.compile(r"^#!.*\b(bash|sh|zsh|ash)\b"), "shell"),
    (re.compile(r"^#!.*\bnode\b"),             "javascript"),
    (re.compile(r"^#!.*\bswift\b"),            "swift"),
    (re.compile(r"^#!.*\bphp\b"),              "php"),
    # Go, C#, Kotlin don't have standard shebang conventions — detected
    # via extension / content only.
]

_EXT_LANG = {
    ".py":    "python",
    ".ipynb": "python",
    ".r":     "r",
    ".R":     "r",
    ".jl":    "julia",
    ".rs":    "rust",
    ".cpp":   "cpp",
    ".cc":    "cpp",
    ".cu":    "cpp",  # CUDA kernel source
    ".h":     "cpp",
    ".hpp":   "cpp",
    ".sh":    "shell",
    ".bash":  "shell",
    ".js":    "javascript",
    ".ts":    "javascript",
    ".mjs":   "javascript",
    ".go":    "go",
    ".cs":    "csharp",
    ".csproj": "csharp",
    ".swift": "swift",
    ".kt":    "kotlin",
    ".kts":   "kotlin",
    ".php":   "php",
}

# Content heuristics. Each entry is (regex, language). Evaluated only
# when neither extension nor shebang produced a hit. Patterns are
# specific enough to avoid false positives across languages — err on
# the side of "unknown" rather than confidently mislabeling.
_CONTENT_HEURISTICS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*import\s+torch\b",                       re.MULTILINE), "python"),
    (re.compile(r"^\s*from\s+transformers\s+import",           re.MULTILINE), "python"),
    (re.compile(r"^\s*(?:def|class|async\s+def)\s+\w+",        re.MULTILINE), "python"),
    (re.compile(r"^\s*library\s*\(\s*torch\s*\)",              re.MULTILINE), "r"),
    (re.compile(r"<-\s*function\s*\(",                          re.MULTILINE), "r"),
    (re.compile(r"^\s*using\s+(CUDA|AMDGPU|Metal|oneAPI|Flux)\b", re.MULTILINE), "julia"),
    (re.compile(r"^\s*function\s+\w+.*\n.*\nend\s*$",           re.MULTILINE), "julia"),
    (re.compile(r"^\s*fn\s+\w+\s*\(",                           re.MULTILINE), "rust"),
    (re.compile(r"^\s*use\s+(wgpu|candle|cudarc)::",            re.MULTILINE), "rust"),
    (re.compile(r"^\s*#include\s*<",                            re.MULTILINE), "cpp"),
    (re.compile(r"^\s*ffmpeg\s+",                               re.MULTILINE), "shell"),
    (re.compile(r"^\s*(?:const|let|var)\s+\w+\s*=",             re.MULTILINE), "javascript"),
    # Go: `package main` is effectively mandatory at the top of any
    # Go source file; very reliable signal.
    (re.compile(r"^\s*package\s+\w+\s*$",                       re.MULTILINE), "go"),
    (re.compile(r"^\s*func\s+\w+\s*\(",                         re.MULTILINE), "go"),
    # C#: `using System;` is the idiomatic top-of-file import.
    (re.compile(r"^\s*using\s+System(?:\.\w+)*\s*;",            re.MULTILINE), "csharp"),
    (re.compile(r"^\s*namespace\s+\w+(?:\.\w+)*",               re.MULTILINE), "csharp"),
    # Swift: import with no trailing semicolon; `func` keyword.
    (re.compile(r"^\s*import\s+(Foundation|SwiftUI|Metal|Accelerate)\b", re.MULTILINE), "swift"),
    (re.compile(r"^\s*func\s+\w+\s*\(.*\)\s*(?:->|\{)",         re.MULTILINE), "swift"),
    # Kotlin: fun keyword + package statement; similar syntax to Swift
    # so we bias toward `fun` over `func`.
    (re.compile(r"^\s*fun\s+\w+\s*\(",                          re.MULTILINE), "kotlin"),
    (re.compile(r"^\s*import\s+kotlin(?:\.\w+)*\s*$",           re.MULTILINE), "kotlin"),
    # PHP: `<?php` is unambiguous.
    (re.compile(r"^\s*<\?php\b"),                                                  "php"),
]


def detect_language(
    source: str,
    *,
    filename: Optional[str] = None,
) -> str:
    """Best-effort identify the language of a source blob.

    Precedence: file extension → shebang → content heuristics → "unknown".
    Returns a stable lowercase tag (python/r/julia/rust/cpp/shell/javascript/unknown)
    used to key into VARIANT_GUIDANCE and LANGUAGE_VARIANT_COMPAT below.
    """
    if filename:
        dot = filename.rfind(".")
        if dot != -1:
            ext = filename[dot:].lower()
            # Preserve .R vs .r ambiguity — .R is R, .r should be too.
            # The dict above folds both.
            lang = _EXT_LANG.get(filename[dot:]) or _EXT_LANG.get(ext)
            if lang:
                return lang

    # Shebang is on line 1 if present.
    head = source[:200]
    for pat, lang in _SHEBANG_LANG:
        if pat.match(head):
            return lang

    for pat, lang in _CONTENT_HEURISTICS:
        if pat.search(source):
            return lang

    return "unknown"


# -----------------------------------------------------------------
# Language × variant guidance
# -----------------------------------------------------------------

# First-class structured guidance fed to the LLM adapter prompt. Each
# entry tells the model the idioms, gotchas, and package names for
# (language, variant). The less specific the entry, the more the
# adapter output has to be taken on faith — we keep languages with
# sparse training signal (rust, cpp) lean so the adapter can fall
# back to a generic "language-agnostic" strategy when needed.
VARIANT_GUIDANCE: Dict[str, Dict[str, str]] = {
    "python": {
        "cuda":     "NVIDIA CUDA. Device string 'cuda' or 'cuda:0'. torch is stock cu124 build. torch.cuda.is_available() gates. This is the hero target — adaptation here is usually a no-op.",
        "rocm":     "AMD ROCm. torch is the ROCm-compiled build. IMPORTANT: the device string remains 'cuda' on ROCm-compiled torch (HIP is a compatibility shim). Prefer device-agnostic selection: device = 'cuda' if torch.cuda.is_available() else 'cpu'. Do NOT call .to('hip') — it's not a valid PyTorch device name.",
        "intel":    "Intel XPU (Arc / Data Center GPU Max / Flex). Needs intel_extension_for_pytorch (ipex). Replace every .to('cuda') with .to('xpu'). Replace torch.cuda.is_available() with torch.xpu.is_available(). torch.cuda.amp.* → torch.xpu.amp.*. Import ipex at the top of the file so it auto-registers the xpu backend.",
        "vulkan":   "Vulkan compute. torch has no native Vulkan device for ML workloads. Fall back to CPU and WARN the user. ncnn/PaddleLite users keep their framework-specific calls.",
        "tpu":      "Google TPU via torch_xla. Replace .to('cuda') with .to(xm.xla_device()) where xm = torch_xla.core.xla_model. torch.cuda API calls are unsupported; use torch_xla equivalents. Add explicit `import torch_xla.core.xla_model as xm` at the top if missing.",
        "npu":      "Neural processing unit via OpenVINO. If the script uses plain torch for inference (import torch), mark INCOMPATIBLE — torch isn't available in the NPU base image. If it already uses openvino.runtime.Core / ov.Core, adapt the device selection to 'NPU' or 'AUTO:NPU'.",
        "cpu":      "CPU-only. Replace device-specific calls with 'cpu'. Remove .half()/torch.float16 calls unless the model explicitly supports CPU fp16. NEVER mark incompatible — every script can run on CPU, possibly slowly.",
        "metal":    "Apple Metal (MPS). Replace .to('cuda') with .to('mps'). Gate via torch.backends.mps.is_available(). MPS has op-coverage gaps — some ops silently fall back to CPU; flag as info if you see unsupported op types.",
        "tensorrt": "NVIDIA TensorRT. For most plain torch scripts this is equivalent to CUDA (build the engine at runtime with torch.onnx.export → trtexec). No device-string rewrite needed. Mark as near-identical to the hero.",
        "groq":     "Groq LPU cloud. Not a local-GPU runtime — the workload invokes a remote API. If the hero does local torch inference, mark INCOMPATIBLE.",
        "fpga":     "FPGA accelerator. Torch not available. Mark INCOMPATIBLE unless the script uses an FPGA-aware framework explicitly.",
    },
    "r": {
        "cuda":     "R torch package. Device string is 'cuda' (same as Python torch). torch::cuda_is_available() gates. This is the hero target when the script uses CUDA.",
        "cpu":      "R torch package with device = 'cpu'. Replace torch_device('cuda') → torch_device('cpu').",
        # All other variants are UNSUPPORTED for R — see LANGUAGE_VARIANT_COMPAT below.
    },
    "julia": {
        "cuda":     "Julia with CUDA.jl. `cu(x)` moves an array to GPU. CUDA.functional() gates availability. Kernels via @cuda.",
        "rocm":     "Julia with AMDGPU.jl. `roc(x)` moves an array to GPU. AMDGPU.functional() gates. @roc launches kernels. Replace `using CUDA` with `using AMDGPU`, `cu(x)` with `roc(x)`, `@cuda` with `@roc`.",
        "intel":    "Julia with oneAPI.jl (less mature than the others). `oneArray(x)` moves data. oneAPI.functional() gates.",
        "metal":    "Julia with Metal.jl. `mtl(x)` moves data. Metal.functional() gates.",
        "cpu":      "Julia default arrays are CPU. Strip `using CUDA`/AMDGPU/oneAPI/Metal and any cu/roc/onearray/mtl wrappers. Replace @cuda/@roc kernel launches with plain for loops.",
        # Julia has no canonical TPU, NPU, Vulkan, TensorRT, Groq, FPGA story → unsupported.
    },
    "shell": {
        "cuda":     "Shell/CLI tools with CUDA hwaccel. For ffmpeg: `-hwaccel cuda -hwaccel_output_format cuda`. Other tools vary — if unsure, keep the invocation but add a CPU fallback behind `command -v` check.",
        "rocm":     "Shell/CLI tools with AMD hwaccel. ffmpeg: limited — most pipelines use VAAPI (`-hwaccel vaapi -vaapi_device /dev/dri/renderD128`) rather than a ROCm-specific path. Fall back to CPU if unsure.",
        "intel":    "Shell/CLI tools on Intel. ffmpeg: VAAPI (`-hwaccel vaapi`) or QSV (`-hwaccel qsv`). QSV is newer and better for Arc.",
        "cpu":      "Plain CPU invocation. Strip any `-hwaccel` / `-c:v *_cuvid` / similar hardware-acceleration flags.",
        # Rest → unsupported for shell.
    },
    "go": {
        "cuda":     "Go with cudarc / Gorgonia / go-cu. cudarc.NewCudaDevice(0) gates availability; tensors created with cudarc.NewCudaSlice(dev, ...). Strip any cpu-only fallback markers.",
        "cpu":      "Go with plain stdlib or pure-Go ML libraries (gorgonia CPU mode, gonum). Remove any cudarc / GPU-specific imports and calls.",
    },
    "swift": {
        "cuda":     "Swift on macOS/Linux cannot target CUDA natively; most Swift ML uses Metal or CoreML. Flag a warning that CUDA on Swift needs a manual CUDA C/C++ bridge — usually mark INCOMPATIBLE unless the user has wired such a bridge.",
        "metal":    "Swift on macOS with Metal (MPSGraph / MLX). Use MTLDevice.default, MPSGraph(device:). This is the Swift-native GPU target.",
        "cpu":      "Swift with plain stdlib / CoreML on CPU. Strip Metal / MPS imports.",
    },
    "csharp": {
        "cuda":     "C# / .NET with ILGPU, ManagedCUDA, or TorchSharp. TorchSharp uses device 'cuda' in a Torch.Device. ILGPU uses Accelerator.CreateCudaAccelerator(). Keep usings and adjust device selection accordingly.",
        "rocm":     "C# / .NET with TorchSharp on ROCm. Same 'cuda' device string (TorchSharp exposes ROCm through CUDA API shim). Limited platform support.",
        "cpu":      "Plain .NET on CPU. Use Torch.Device.CPU / ILGPU Accelerator.CreateCPUAccelerator().",
    },
    "kotlin": {
        "cuda":     "Kotlin / JVM with KInference, DJL (Deep Java Library), or JCuda. DJL: Device.gpu(0). JCuda: JCuda.init(). Less common on CUDA so emit a warning to double-check dependency wiring.",
        "cpu":      "Kotlin / JVM on CPU. DJL: Device.cpu(). Strip JCuda / CUDA-specific imports.",
    },
    "php": {
        "cpu":      "PHP is almost always CPU. ML-adjacent libs (php-ml, rubix/ml) are pure-PHP CPU code. Strip any GPU-flavoured imports if present.",
    },
    # rust / cpp / javascript / unknown: fall through to a generic
    # prompt (see _guidance_for fallback) that tells the LLM to do a
    # best-effort adaptation without framework-specific hints.
}


# Compatibility matrix. Values:
#   "native"      → adapter runs; we expect a clean rewrite
#   "limited"     → adapter runs; expect warnings about reduced features
#   "unsupported" → adapter is skipped; variant marked incompatible
#
# Rule of thumb: if a reasonable open-source package exists to make
# (language, variant) work, it's native; if only a degraded CPU-ish
# path exists, limited; if literally nobody runs that combination,
# unsupported.
LANGUAGE_VARIANT_COMPAT: Dict[str, Dict[str, str]] = {
    "python": {
        "cuda": "native", "rocm": "native", "intel": "native",
        "tpu": "native", "npu": "native", "cpu": "native", "metal": "native",
        "tensorrt": "native",
        "vulkan": "limited", "groq": "limited",
        "fpga": "unsupported",
    },
    "r": {
        "cuda": "native", "cpu": "native",
        # Everything else is not wired up in R's torch package or is
        # non-existent in the R ecosystem.
        "rocm": "unsupported", "intel": "unsupported", "metal": "unsupported",
        "tpu": "unsupported", "npu": "unsupported",
        "vulkan": "unsupported", "tensorrt": "unsupported",
        "groq": "unsupported", "fpga": "unsupported",
    },
    "julia": {
        "cuda": "native", "rocm": "native", "metal": "native", "cpu": "native",
        "intel": "limited",        # oneAPI.jl exists but is young
        "tensorrt": "limited",     # via CUDA.jl's wrappers
        "tpu": "unsupported",      # no torch_xla-equivalent in Julia
        "npu": "unsupported",      # no OpenVINO.jl
        "vulkan": "unsupported",
        "groq": "unsupported",
        "fpga": "unsupported",
    },
    "shell": {
        "cuda": "limited", "rocm": "limited", "intel": "limited",
        "cpu": "native",
        # No coherent "shell on TPU/NPU/Vulkan/Metal/etc." story.
        "tpu": "unsupported", "npu": "unsupported", "vulkan": "unsupported",
        "metal": "unsupported", "tensorrt": "unsupported",
        "groq": "unsupported", "fpga": "unsupported",
    },
    "go": {
        "cuda": "native", "cpu": "native",
        # Go's ML story is thin on everything else; mark unsupported
        # rather than risk the LLM inventing a package.
        "rocm": "unsupported", "intel": "unsupported", "metal": "unsupported",
        "tpu": "unsupported", "npu": "unsupported", "vulkan": "unsupported",
        "tensorrt": "unsupported", "groq": "unsupported", "fpga": "unsupported",
    },
    "swift": {
        "metal": "native", "cpu": "native",
        "cuda": "limited",  # only via manual CUDA bridge
        # Everything else is unsupported — Swift-on-ROCm/Intel/TPU/NPU
        # is essentially nonexistent.
        "rocm": "unsupported", "intel": "unsupported", "tpu": "unsupported",
        "npu": "unsupported", "vulkan": "unsupported",
        "tensorrt": "unsupported", "groq": "unsupported", "fpga": "unsupported",
    },
    "csharp": {
        "cuda": "native", "cpu": "native",
        "rocm": "limited",  # TorchSharp compat shim
        "intel": "unsupported", "metal": "unsupported", "tpu": "unsupported",
        "npu": "unsupported", "vulkan": "unsupported",
        "tensorrt": "limited", "groq": "unsupported", "fpga": "unsupported",
    },
    "kotlin": {
        "cuda": "limited",  # DJL / JCuda, workable but niche
        "cpu": "native",
        "rocm": "unsupported", "intel": "unsupported", "metal": "unsupported",
        "tpu": "unsupported", "npu": "unsupported", "vulkan": "unsupported",
        "tensorrt": "unsupported", "groq": "unsupported", "fpga": "unsupported",
    },
    "php": {
        "cpu": "native",
        # PHP has no real GPU story.
        "cuda": "unsupported", "rocm": "unsupported", "intel": "unsupported",
        "metal": "unsupported", "tpu": "unsupported", "npu": "unsupported",
        "vulkan": "unsupported", "tensorrt": "unsupported",
        "groq": "unsupported", "fpga": "unsupported",
    },
    # rust / cpp / javascript / unknown are passthrough: the adapter
    # runs with a generic prompt and relies on the LLM's own judgement
    # about portability. See _compat_for() below.
}


_PASSTHROUGH_LANGUAGES = {"rust", "cpp", "javascript", "unknown"}


# Compat tier ordering for intersection math. intersect() returns the
# *worst* tier across a list — if one file is unsupported, the variant
# is unsupported for the whole node; if everything is native, the
# variant is native; limited in between.
_COMPAT_RANK = {"native": 2, "limited": 1, "unsupported": 0}


def _compat_for(language: str, variant: str) -> str:
    """Return "native" / "limited" / "unsupported" for the (language,
    variant) pair. Passthrough languages default to "limited" — we
    still try the adapter but expect mixed quality and warn the user.
    """
    if language in _PASSTHROUGH_LANGUAGES:
        return "limited"
    return LANGUAGE_VARIANT_COMPAT.get(language, {}).get(variant, "unsupported")


def intersect_compat(tiers: List[str]) -> str:
    """Worst-of across a list of compat tiers. If any input is
    "unsupported", result is "unsupported" (one blocked file blocks
    the whole node). Otherwise the minimum of "native" / "limited".
    Empty input returns "unsupported" defensively.
    """
    if not tiers:
        return "unsupported"
    rank = min(_COMPAT_RANK.get(t, 0) for t in tiers)
    for name, r in _COMPAT_RANK.items():
        if r == rank:
            return name
    return "unsupported"


def _guidance_for(language: str, variant: str) -> str:
    """Prompt-ready guidance string for (language, variant)."""
    lang_table = VARIANT_GUIDANCE.get(language, {})
    specific = lang_table.get(variant)
    if specific:
        return specific
    # Passthrough fallback — the LLM has to reason from first principles.
    return (
        f"No platform-curated guidance for language='{language}', "
        f"variant='{variant}'. Make a conservative best-effort "
        f"adaptation. If you can't tell how to retarget without "
        f"knowing framework-specific details, leave the code as-is "
        f"and emit a 'warn' message explaining what a human reviewer "
        f"should check. Do NOT invent package names or APIs you're "
        f"not certain exist."
    )


# -----------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------

@dataclass
class SourceChange:
    """A single rewrite made by the adapter."""
    before: str
    after: str
    line: int = 0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"before": self.before, "after": self.after,
                "line": self.line, "reason": self.reason}


@dataclass
class SourceWarning:
    """A user-visible warning surfaced by the adapter."""
    severity: str  # "info" | "warn" | "fatal"
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {"severity": self.severity, "message": self.message}


@dataclass
class AdaptResult:
    """Everything produced for one variant."""
    variant: str
    language: str
    adapted_source: str
    compat: str                     # native | limited | unsupported
    changes: List[SourceChange] = field(default_factory=list)
    warnings: List[SourceWarning] = field(default_factory=list)
    incompatible: bool = False
    incompatible_reason: str = ""
    llm_used: bool = True           # False means hero returned as-is

    def to_dict(self) -> Dict[str, Any]:
        return {
            "variant": self.variant,
            "language": self.language,
            "adapted_source": self.adapted_source,
            "compat": self.compat,
            "changes": [c.to_dict() for c in self.changes],
            "warnings": [w.to_dict() for w in self.warnings],
            "incompatible": self.incompatible,
            "incompatible_reason": self.incompatible_reason,
            "llm_used": self.llm_used,
        }


# -----------------------------------------------------------------
# LLM I/O
# -----------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_adapter_response(raw: str, variant: str) -> Dict[str, Any]:
    """Lenient JSON extractor (direct → code-fenced → first balanced
    block → {}). The adapter prompt asks for pure JSON; this exists for
    when the model slips a markdown fence or a sentence of preamble.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = raw.find("{")
    if start >= 0:
        depth = 0
        for i, ch in enumerate(raw[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start:i + 1])
                    except json.JSONDecodeError:
                        break
    logger.warning("Adapter LLM returned unparseable JSON for variant=%s", variant)
    return {}


def _diff_changes(hero: str, adapted: str) -> List[SourceChange]:
    """Derive compact per-line changes when the LLM omitted the
    `changes` list. Whitespace-only shuffles are filtered out.
    """
    out: List[SourceChange] = []
    hero_lines = hero.splitlines()
    adapted_lines = adapted.splitlines()
    sm = difflib.SequenceMatcher(a=hero_lines, b=adapted_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        before = "\n".join(hero_lines[i1:i2]).strip()
        after = "\n".join(adapted_lines[j1:j2]).strip()
        if not before and not after:
            continue
        out.append(SourceChange(
            before=before[:400],
            after=after[:400],
            line=i1 + 1,
        ))
    return out


def _validate_python(source: str) -> Optional[str]:
    """Parse the source as Python. Return error message on syntax
    failure, None on success. Used to reject LLM outputs that don't
    even compile — callers fall back to the hero in that case.
    """
    try:
        compile(source, "<adapted>", "exec")
        return None
    except SyntaxError as e:
        return f"{e.msg} at line {e.lineno}"


def _build_prompts(
    hero_source: str,
    language: str,
    variant: str,
    filename: str,
) -> Tuple[str, str]:
    """Compose system + user prompts. Both strings are plain text —
    providers that have system-role messages use them properly;
    providers that don't concatenate internally.
    """
    guidance = _guidance_for(language, variant)
    compat = _compat_for(language, variant)

    system_prompt = (
        "You adapt source code written for one accelerator backend to "
        "run on another. Your only output is a single JSON object — no "
        "prose, no markdown fences, no code blocks around it.\n\n"
        "REQUIRED OUTPUT SHAPE:\n"
        "{\n"
        '  "adapted_source": "<FULL rewritten source, not a diff>",\n'
        '  "changes":  [ {"before": "...", "after": "...", "line": 42, "reason": "..."} ],\n'
        '  "warnings": [ {"severity": "info|warn|fatal", "message": "..."} ],\n'
        '  "incompatible": false,\n'
        '  "incompatible_reason": ""\n'
        "}\n\n"
        "RULES:\n"
        "- `adapted_source` is the COMPLETE rewritten file. Preserve "
        "  control flow, variable names, and comments; change only what "
        "  the target backend requires.\n"
        "- If the workload fundamentally cannot run on the target "
        "  (wrong framework, no package available, etc.), set "
        "  `incompatible=true`, explain why in `incompatible_reason`, "
        "  and return the hero as `adapted_source` (it won't be built).\n"
        "- List every non-trivial rewrite in `changes`. Don't list "
        "  whitespace reshuffles.\n"
        "- Warnings: 'fatal' for hard blockers, 'warn' for "
        "  reduced-performance paths (e.g. CPU fallback), 'info' for "
        "  anything else a reviewer should notice.\n"
        "- Never invent package names or APIs you aren't certain exist. "
        "  When in doubt, leave the code alone and emit a 'warn'.\n"
    )

    user_prompt = (
        f"SOURCE LANGUAGE: {language}\n"
        f"SOURCE FILENAME: {filename}\n"
        f"TARGET VARIANT:  {variant}\n"
        f"COMPATIBILITY:   {compat}\n\n"
        f"GUIDANCE:\n{guidance}\n\n"
        f"HERO SOURCE (targeting the platform's default backend):\n"
        f"```\n{hero_source}\n```\n\n"
        "Return ONLY the JSON object."
    )
    return system_prompt, user_prompt


async def _run_adapter_llm(
    hero_source: str,
    language: str,
    variant: str,
    filename: str,
    provider: Any,
) -> Dict[str, Any]:
    system_prompt, user_prompt = _build_prompts(hero_source, language, variant, filename)
    chunks: List[str] = []
    try:
        async for chunk in provider.stream(system_prompt, user_prompt):
            chunks.append(chunk)
    except Exception as e:  # noqa: BLE001
        logger.warning("Adapter LLM stream failed for variant=%s: %s", variant, e)
        return {}
    return _parse_adapter_response("".join(chunks), variant)


# -----------------------------------------------------------------
# Public API
# -----------------------------------------------------------------

def _adapt_result_from_cache(payload: Dict[str, Any]) -> AdaptResult:
    """Rehydrate an AdaptResult from its cached JSON dict. Mirrors
    `AdaptResult.to_dict()` — kept in one place so we notice when
    fields drift."""
    return AdaptResult(
        variant=payload.get("variant", ""),
        language=payload.get("language", ""),
        adapted_source=payload.get("adapted_source", ""),
        compat=payload.get("compat", "limited"),
        changes=[
            SourceChange(
                before=str(c.get("before", "")),
                after=str(c.get("after", "")),
                line=int(c.get("line") or 0),
                reason=str(c.get("reason", "")),
            )
            for c in payload.get("changes", []) if isinstance(c, dict)
        ],
        warnings=[
            SourceWarning(
                severity=str(w.get("severity", "info")),
                message=str(w.get("message", "")),
            )
            for w in payload.get("warnings", []) if isinstance(w, dict)
        ],
        incompatible=bool(payload.get("incompatible", False)),
        incompatible_reason=payload.get("incompatible_reason", "") or "",
        llm_used=bool(payload.get("llm_used", True)),
    )


async def adapt_variant_async(
    hero_source: str,
    variant: str,
    *,
    language: Optional[str] = None,
    filename: str = "",
    provider: Optional[Any] = None,
    tenant_id: Optional[int] = None,
) -> AdaptResult:
    """Adapt a single (hero_source, variant) to produce a variant-
    specific source. Async so a caller can fan out with asyncio.gather.

    language: detected from `filename` + content heuristics if omitted.
    provider: reuses an existing LLMProvider if the caller has one open,
              otherwise we build one from LLMConfig.from_env() — the
              same code path the rest of the wizard uses.
    tenant_id: threaded into the cache key so a per-tenant flush
               (Dashboard "Clear adapter cache") isolates cache resets
               to the asker. Absent tenant_id → cache disabled for
               that call.
    """
    lang = language or detect_language(hero_source, filename=filename)
    compat = _compat_for(lang, variant)

    # Fast paths — no LLM call needed.
    if compat == "unsupported":
        return AdaptResult(
            variant=variant,
            language=lang,
            adapted_source=hero_source,
            compat=compat,
            incompatible=True,
            incompatible_reason=(
                f"{lang!r} source cannot target variant {variant!r}: "
                f"no supported framework/runtime for this combination."
            ),
            llm_used=False,
        )
    if not hero_source.strip():
        return AdaptResult(
            variant=variant,
            language=lang,
            adapted_source=hero_source,
            compat=compat,
            warnings=[SourceWarning("info", "Hero source empty; skipping adaptation.")],
            llm_used=False,
        )

    # Cache lookup. Key depends on tenant_id, language, variant,
    # filename, body, and PROMPT_TEMPLATE_VERSION. Misses fall through
    # to the LLM call below; hits short-circuit with `llm_used=False`
    # flipped (the cached result may have llm_used=True from its
    # original fetch; we leave that as the historical truth and let
    # telemetry distinguish cache-hit vs live-call separately).
    cache_key = _cache_key(
        tenant_id=tenant_id, language=lang, variant=variant,
        filename=filename, body=hero_source,
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        try:
            return _adapt_result_from_cache(cached)
        except Exception as e:  # noqa: BLE001
            logger.warning("Adapter cache corrupt for key=%s: %s", cache_key, e)
            # Fall through to a fresh LLM call; don't block on bad cache.

    if provider is None:
        try:
            cfg = LLMConfig.from_env()
            cfg.validate()
            provider = get_provider(cfg)
        except Exception as e:  # noqa: BLE001
            logger.warning("No LLM provider available for adapter: %s", e)
            return AdaptResult(
                variant=variant,
                language=lang,
                adapted_source=hero_source,
                compat=compat,
                warnings=[SourceWarning(
                    "warn",
                    f"Per-variant adapter unavailable ({e}); using hero source unchanged.",
                )],
                llm_used=False,
            )

    parsed = await _run_adapter_llm(hero_source, lang, variant, filename, provider)
    if not parsed:
        return AdaptResult(
            variant=variant,
            language=lang,
            adapted_source=hero_source,
            compat=compat,
            warnings=[SourceWarning(
                "warn",
                "Adapter LLM produced no valid JSON; using hero source unchanged.",
            )],
        )

    adapted_source = parsed.get("adapted_source") or hero_source
    incompatible = bool(parsed.get("incompatible", False))
    incompatible_reason = parsed.get("incompatible_reason", "") or ""

    # Python syntax validation. Other languages skip this — their
    # parsers aren't in the containerizer's base image and a full
    # subprocess spawn per variant is too slow for a preview pass.
    if lang == "python" and not incompatible:
        syntax_err = _validate_python(adapted_source)
        if syntax_err:
            return AdaptResult(
                variant=variant,
                language=lang,
                adapted_source=hero_source,
                compat=compat,
                warnings=[SourceWarning(
                    "warn",
                    f"Adapter produced invalid Python ({syntax_err}); "
                    f"reverted to hero source.",
                )],
            )

    # Extract changes / warnings from LLM response with tolerant parsing.
    changes: List[SourceChange] = []
    for c in (parsed.get("changes") or []):
        if not isinstance(c, dict):
            continue
        changes.append(SourceChange(
            before=str(c.get("before", ""))[:400],
            after=str(c.get("after", ""))[:400],
            line=int(c.get("line") or 0),
            reason=str(c.get("reason", ""))[:240],
        ))
    if not changes and adapted_source.strip() != hero_source.strip():
        changes = _diff_changes(hero_source, adapted_source)

    warnings: List[SourceWarning] = []
    for w in (parsed.get("warnings") or []):
        if not isinstance(w, dict):
            continue
        severity = str(w.get("severity", "info")).lower()
        if severity not in {"info", "warn", "fatal"}:
            severity = "info"
        warnings.append(SourceWarning(severity, str(w.get("message", ""))[:400]))

    # Augment the warning list with a platform-level note whenever the
    # compat tier is "limited" so the UI always shows something on the
    # row — operators shouldn't have to dig into the diff to find out
    # "why is this one in yellow".
    if compat == "limited" and not any(w.severity != "info" for w in warnings):
        warnings.append(SourceWarning(
            "info",
            f"{lang} on {variant} has limited platform support; reduced "
            f"features or CPU fallback may apply.",
        ))

    result = AdaptResult(
        variant=variant,
        language=lang,
        adapted_source=adapted_source if not incompatible else hero_source,
        compat=compat,
        changes=changes,
        warnings=warnings,
        incompatible=incompatible,
        incompatible_reason=incompatible_reason,
    )

    # Cache write. Transient Redis hiccups are swallowed (see _cache_set)
    # so a cache failure never blocks the live build.
    _cache_set(cache_key, result.to_dict())

    return result


async def adapt_variants_async(
    hero_source: str,
    variants: List[str],
    *,
    language: Optional[str] = None,
    filename: str = "",
    provider: Optional[Any] = None,
    concurrency: int = 4,
    tenant_id: Optional[int] = None,
) -> Dict[str, AdaptResult]:
    """Fan out adaptation across a variant list with bounded concurrency.
    Caller passes an already-built provider to amortise client setup
    across N calls. Default concurrency=4 keeps wall-clock at ~(N/4)×LLM
    latency while staying well under OpenAI/Anthropic per-minute limits.
    """
    lang = language or detect_language(hero_source, filename=filename)
    if provider is None:
        try:
            cfg = LLMConfig.from_env()
            cfg.validate()
            provider = get_provider(cfg)
        except Exception:  # noqa: BLE001
            provider = None  # adapt_variant_async will short-circuit

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(v: str) -> Tuple[str, AdaptResult]:
        async with sem:
            return v, await adapt_variant_async(
                hero_source, v,
                language=lang, filename=filename, provider=provider,
                tenant_id=tenant_id,
            )

    results = await asyncio.gather(*[_one(v) for v in variants])
    return dict(results)


def adapt_variants(
    hero_source: str,
    variants: List[str],
    *,
    language: Optional[str] = None,
    filename: str = "",
    concurrency: int = 4,
) -> Dict[str, AdaptResult]:
    """Sync wrapper for tests / sync callers. The Containerizer's
    create_albedo_pattern_tool is already async and calls
    adapt_variants_async directly.
    """
    return asyncio.run(adapt_variants_async(
        hero_source, variants,
        language=language, filename=filename, concurrency=concurrency,
    ))


# -----------------------------------------------------------------
# Multi-file API — the real v1 entry point
# -----------------------------------------------------------------

@dataclass
class SourceFile:
    """One file in the user's node. A node can have N files mixing
    languages (e.g. main.py orchestrates Rscript analyze.R)."""
    filename: str
    body: str
    language: str = ""          # detected if empty
    is_entrypoint: bool = False  # exactly one file should be the entrypoint

    def detected_language(self) -> str:
        return self.language or detect_language(self.body, filename=self.filename)


@dataclass
class NodeAdaptResult:
    """Everything produced for a single variant across ALL files in
    a multi-file node. The per-variant tile in the wizard Preview
    renders from this struct: one sub-tab per file in `files`, plus
    metadata for the tile header."""
    variant: str
    compat: str                                   # worst-of across files
    files: Dict[str, AdaptResult]                 # {filename: AdaptResult}
    incompatible: bool = False
    incompatible_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "variant": self.variant,
            "compat": self.compat,
            "files": {fn: r.to_dict() for fn, r in self.files.items()},
            "incompatible": self.incompatible,
            "incompatible_reason": self.incompatible_reason,
        }


def compat_for_node(files: List[SourceFile], variant: str) -> str:
    """Worst-of compat across every file in a node for a given variant.
    A variant is only buildable for the whole node if *every* file can
    run on it (intersection, not union)."""
    tiers = [_compat_for(f.detected_language(), variant) for f in files]
    return intersect_compat(tiers)


async def adapt_node_variant_async(
    files: List[SourceFile],
    variant: str,
    *,
    provider: Optional[Any] = None,
    concurrency: int = 4,
    tenant_id: Optional[int] = None,
) -> NodeAdaptResult:
    """Adapt every file in a node for one target variant. Concurrency
    bounds per-file fan-out inside this single variant; the outer
    `adapt_node_async` also bounds across variants.
    """
    node_compat = compat_for_node(files, variant)

    # Short-circuit: any file unsupported → whole variant unbuildable.
    if node_compat == "unsupported":
        blockers = [
            f"{f.filename} ({f.detected_language()})"
            for f in files
            if _compat_for(f.detected_language(), variant) == "unsupported"
        ]
        reason = (
            f"variant {variant!r} not supported for: "
            + ", ".join(blockers) + "."
        )
        return NodeAdaptResult(
            variant=variant,
            compat=node_compat,
            files={
                f.filename: AdaptResult(
                    variant=variant,
                    language=f.detected_language(),
                    adapted_source=f.body,
                    compat=_compat_for(f.detected_language(), variant),
                    incompatible=True,
                    incompatible_reason=reason,
                    llm_used=False,
                )
                for f in files
            },
            incompatible=True,
            incompatible_reason=reason,
        )

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(f: SourceFile) -> Tuple[str, AdaptResult]:
        async with sem:
            return f.filename, await adapt_variant_async(
                f.body, variant,
                language=f.detected_language(),
                filename=f.filename,
                provider=provider,
                tenant_id=tenant_id,
            )

    pairs = await asyncio.gather(*[_one(f) for f in files])
    per_file = dict(pairs)
    return NodeAdaptResult(
        variant=variant,
        compat=node_compat,
        files=per_file,
    )


async def adapt_node_async(
    files: List[SourceFile],
    variants: List[str],
    *,
    provider: Optional[Any] = None,
    variant_concurrency: int = 4,
    file_concurrency: int = 4,
    tenant_id: Optional[int] = None,
) -> Dict[str, NodeAdaptResult]:
    """Fan out adaptation across (variant × file). Total LLM calls =
    len(files) × len(variants), minus (file, variant) pairs where the
    language is unsupported for that variant (short-circuited). Two
    independent concurrency limits — outer across variants, inner
    across files within a variant — keep the wall-clock bounded
    without flooding the LLM provider.
    """
    if provider is None:
        try:
            cfg = LLMConfig.from_env()
            cfg.validate()
            provider = get_provider(cfg)
        except Exception:  # noqa: BLE001
            provider = None

    sem = asyncio.Semaphore(max(1, variant_concurrency))

    async def _one(v: str) -> Tuple[str, NodeAdaptResult]:
        async with sem:
            return v, await adapt_node_variant_async(
                files, v, provider=provider, concurrency=file_concurrency,
                tenant_id=tenant_id,
            )

    pairs = await asyncio.gather(*[_one(v) for v in variants])
    return dict(pairs)


def adapt_node(
    files: List[SourceFile],
    variants: List[str],
    *,
    variant_concurrency: int = 4,
    file_concurrency: int = 4,
) -> Dict[str, NodeAdaptResult]:
    """Sync wrapper around adapt_node_async for tests and any
    non-async caller."""
    return asyncio.run(adapt_node_async(
        files, variants,
        variant_concurrency=variant_concurrency,
        file_concurrency=file_concurrency,
    ))


if __name__ == "__main__":
    # Smoke test: stdin = hero source, argv[1] = variant, argv[2] = filename.
    import sys
    variant = sys.argv[1] if len(sys.argv) > 1 else "intel"
    filename = sys.argv[2] if len(sys.argv) > 2 else ""
    hero = sys.stdin.read()
    result = asyncio.run(adapt_variant_async(hero, variant, filename=filename))
    json.dump(result.to_dict(), sys.stdout, indent=2)
    print()
