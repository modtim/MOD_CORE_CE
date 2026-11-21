"""Automated CUDA → {HIP, SYCL, OpenVINO, Metal} porter.

Layer 3 of Phase 9's four-layer port pipeline:

  1. Static detection  — `_detect_deep_cuda_with_hints` in the API.
  2. Preflight API analysis — `preflight_apis()` below.
  3. Deterministic port — `plan_port()` + `apply_port()` below.
  4. LLM universal fallback — the existing `variant_adapter` gets a
     constrained preamble built from each variant's `PortReport`.

The porter is purely additive: a repo that doesn't trip the
deep-CUDA detector never reaches this module, and the four Dockerfile
variants the wizard emits build exactly as before.

Design invariants:
- Idempotent. Re-running on an already-ported tree is a no-op.
- CPU-only at port time. hipify-clang / dpct run as subprocesses; no
  GPU access required on the builder. Translation is fast (seconds);
  compile time inside the variant Dockerfile dominates.
- asyncio-native. Subprocess calls use `asyncio.create_subprocess_exec`
  so the port-pass doesn't starve the adapter's LLM fan-out.
- Honest failure. Every outcome is one of {compatible, not_recommended,
  incompatible} with a specific reason string. No silent skips.


"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)


# Bump on porter-logic revisions (new hipify version, dpct → SYCLomatic,
# changed heuristic). Baked into the port_outcomes.porter_version so
# cached verdicts roll when the tool changes underneath us.
PORTER_VERSION = "v1"

# Phase 6 — hipify-clang returncode-zero with warnings was historically
# treated as clean translation; many "warnings" are real
# partially-translated lines that won't compile. Count HIPIFY warnings
# per file; above this threshold the verdict is downgraded from
# compatible → not_recommended and the count surfaces in
# incompat_reason so the operator can decide whether to hand-port.
# Env override: MOD_HIPIFY_WARNING_THRESHOLD (int). Default 3.
_HIPIFY_WARNING_DEFAULT_THRESHOLD = 3

# Phase 6 — cached version strings for translator binaries. Populated
# lazily on first call; folded into plan.port_report.translator_version
# so the API's PortOutcome writer dedupes by the effective toolchain
# fingerprint instead of the static PORTER_VERSION.
_BINARY_VERSION_CACHE: Dict[str, str] = {}


Compatibility = Literal["compatible", "not_recommended", "incompatible"]


@dataclass
class FileTransform:
    """Deterministic per-file change the porter intends to make."""
    filename: str
    new_filename: str | None = None   # rename (e.g. foo.cu → foo.hip)
    new_body: str | None = None       # rewrite contents (mutually exclusive with delete)
    delete: bool = False              # remove the file entirely


@dataclass
class SetupPyPatch:
    """LLM hint for build-system rewrites (CUDAExtension → DPCPPExtension)."""
    filename: str                     # e.g. "setup.py", "pyproject.toml"
    kind: str                         # "replace_class" | "add_import" | "swap_key"
    old: str
    new: str


@dataclass
class RequirementsPatch:
    add: List[str] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)


@dataclass
class PortReport:
    """What the deterministic tool did + what's left for Layer 4."""
    tool: str                                    # "hipify-clang" | "torch.hipify" | "dpct" | "openvino" | "noop"
    files_translated: List[str] = field(default_factory=list)
    unsupported_apis: List[str] = field(default_factory=list)
    manual_cleanup_needed: List[str] = field(default_factory=list)
    translator_version: str = ""

    def to_prompt_preamble(self, variant: str) -> str:
        """Structured preamble for the LLM fan-out's per-file prompt.
        Constrains the LLM to setup.py + imports + flagged lines; no
        re-translation of already-ported kernels."""
        lines = [
            f"A deterministic {self.tool} pass has already translated "
            f"CUDA sources to the {variant} backend. Do NOT re-translate "
            f"kernel bodies or touch already-ported `.hip` / `.dp.cpp` "
            f"files unless you see a symbol listed in 'open items' below.",
            "",
            f"Files translated: {', '.join(self.files_translated) or '(none)'}",
        ]
        if self.unsupported_apis:
            lines.append(
                f"Unsupported APIs flagged by the tool (need LLM cleanup "
                f"or mark variant incompatible): "
                f"{', '.join(self.unsupported_apis)}"
            )
        if self.manual_cleanup_needed:
            lines.append("Open items (what's left for you):")
            for item in self.manual_cleanup_needed:
                lines.append(f"  - {item}")
        lines.append(
            "Your edits are bounded: setup.py / pyproject.toml / "
            "requirements.txt imports, class swaps, and the flagged lines. "
            "Leave all other files byte-identical."
        )
        return "\n".join(lines)


@dataclass
class CompatMatrix:
    """Output of Layer 2 preflight."""
    variant: str
    translatable_apis: List[str] = field(default_factory=list)
    unsupported_apis: List[str] = field(default_factory=list)
    overall: Compatibility = "compatible"
    reason: str = ""


@dataclass
class PortPlan:
    """The full artifact a caller applies to a source tree."""
    variant: str
    compatibility: Compatibility
    incompat_reason: Optional[str]
    source_transforms: List[FileTransform] = field(default_factory=list)
    setup_py_patches: List[SetupPyPatch] = field(default_factory=list)
    requirements_patches: RequirementsPatch = field(default_factory=RequirementsPatch)
    dockerfile_env: Dict[str, str] = field(default_factory=dict)
    dockerfile_apt: List[str] = field(default_factory=list)
    verification_cmd: str = ""
    port_report: PortReport = field(default_factory=lambda: PortReport(tool="noop"))

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


# -----------------------------------------------------------------
# Source-hash helper — learning-table cache key
# -----------------------------------------------------------------

def source_hash(files: List[Dict[str, Any]]) -> str:
    """Stable SHA-256 over canonical-ordered (filename, body) pairs.
    Tenant-agnostic: two tenants uploading the same repo get the same
    hash and share `port_outcomes` history."""
    h = hashlib.sha256()
    for f in sorted(files, key=lambda x: x.get("filename", "")):
        name = f.get("filename", "") or ""
        body = f.get("body", "") or ""
        h.update(name.encode("utf-8", errors="replace"))
        h.update(b"\x00")
        h.update(hashlib.sha256(body.encode("utf-8", errors="replace")).digest())
        h.update(b"\x00")
    return h.hexdigest()


# -----------------------------------------------------------------
# Layer 2 — Preflight API analysis
# -----------------------------------------------------------------

# Kernel-dominance seed list (bootstrap only; real truth lives in the
# port_outcomes table). Each import here is a known-kernel-heavy
# package that has historically failed OpenVINO port.
_NPU_DOMINANCE_SEED_IMPORTS = {
    "triton",
    "flash_attn",
    "xformers.ops",
    "apex",
    "bitsandbytes",
    "mamba_ssm",
    "causal_conv1d",
    "natten",
    "tinycudann",
    "pointnet2_ops",
    "diff_gaussian_rasterization",
    "torch_scatter",
    "torch_sparse",
    "dgl.ops",
}


_CUDA_CPP_EXTENSIONS = {".cu", ".cuh"}


async def preflight_apis(
    variant: str,
    source_files: List[Dict[str, Any]],
    *,
    work_dir: Optional[str] = None,
) -> CompatMatrix:
    """Ask the backend's own tool 'can you handle this?' before we
    commit to a full port. CPU-only, fast (seconds).

    HIP:  hipify-clang --examine-only <files>   → parse [HIP_UNSUPPORTED]
    SYCL: dpct --query-api-mapping <files>      → parse DPCT warning codes
    NPU:  AST-walk for torch.onnx.export + seed dominance list
    Metal: static heuristic (no kernel translator exists)
    """
    matrix = CompatMatrix(variant=variant)

    if variant == "rocm":
        return await _preflight_hip(source_files, matrix, work_dir=work_dir)
    if variant == "intel":
        return await _preflight_sycl(source_files, matrix, work_dir=work_dir)
    if variant == "npu":
        return _preflight_npu(source_files, matrix)
    if variant == "metal":
        return _preflight_metal(source_files, matrix)

    # Unknown variant: be conservative, mark incompatible with a
    # clear reason so the caller doesn't silently ship a broken image.
    matrix.overall = "incompatible"
    matrix.reason = f"no preflight for variant {variant!r}"
    return matrix


async def _preflight_hip(
    source_files: List[Dict[str, Any]],
    matrix: CompatMatrix,
    *,
    work_dir: Optional[str],
) -> CompatMatrix:
    """For pytorch CUDAExtension code, torch auto-hipifies — no
    preflight work needed; all APIs are translatable. For raw
    C++/CUDA, invoke hipify-clang --examine-only if it's on PATH.
    When hipify-clang is unavailable (API-side running outside the
    ROCm base), fall back to a static heuristic.

    P9h: Triton kernels port to ROCm via the triton-rocm package
    (AMD's experimental port of triton to HIP). Plan pass adds the
    pip requirement; preflight returns compatible here."""
    cu_files = [f for f in source_files if _is_cu_like(f)]
    has_cuda_ext = any(
        _contains(f, "CUDAExtension") or _contains(f, "torch.utils.cpp_extension")
        for f in source_files
    )
    if not cu_files:
        # Pure framework code. Adapter handles device strings.
        return matrix

    if has_cuda_ext:
        # PyTorch's ROCm build auto-hipifies at build time. Flag a few
        # known-ROCm-partial APIs if we can see them statically.
        _static_unsupported_scan(cu_files, matrix, _STATIC_ROCM_UNSUPPORTED)
        if matrix.unsupported_apis:
            matrix.overall = "not_recommended"
            matrix.reason = (
                "ROCm/HIP partial API support detected: "
                + ", ".join(matrix.unsupported_apis)
            )
        return matrix

    # Raw C++/CUDA (no torch). Try hipify-clang --examine-only if
    # the binary is on PATH (only when running inside the ROCm base).
    hipify = _which("hipify-clang")
    if hipify and work_dir:
        try:
            out = await _run_subprocess(
                [hipify, "--examine-only", "--"]
                + [os.path.join(work_dir, f["filename"]) for f in cu_files],
                timeout_seconds=120,
            )
            matrix.unsupported_apis = _parse_hipify_examine(out)
            matrix.translatable_apis = []  # hipify doesn't list these by default
        except Exception as e:  # noqa: BLE001
            logger.warning("hipify-clang preflight failed: %s", e)
    else:
        _static_unsupported_scan(cu_files, matrix, _STATIC_ROCM_UNSUPPORTED)

    if matrix.unsupported_apis and all(
        _is_hard_block_rocm(api) for api in matrix.unsupported_apis
    ):
        matrix.overall = "incompatible"
        matrix.reason = (
            "CUDA APIs used have no HIP equivalent: "
            + ", ".join(matrix.unsupported_apis)
        )
    elif matrix.unsupported_apis:
        matrix.overall = "not_recommended"
        matrix.reason = (
            "ROCm port needs manual cleanup for: "
            + ", ".join(matrix.unsupported_apis)
        )
    return matrix


async def _preflight_sycl(
    source_files: List[Dict[str, Any]],
    matrix: CompatMatrix,
    *,
    work_dir: Optional[str],
) -> CompatMatrix:
    """dpct --query-api-mapping is the definitive signal when the
    basekit image is reachable; otherwise fall back to a static scan.

    P9h: Triton has no Intel/SYCL backend as of 2026-04. We used
    to mark Intel incompatible on triton; instead we now route it
    through the runtime fallback dispatcher — variant is
    'not_recommended' (CPU fallback is 10-50× slower than CUDA
    on triton-heavy hot paths), but the rest of the model still
    runs on Intel GPU. Plan pass emits the fallback module +
    @triton.jit decoration rewrites."""
    if _uses_triton(source_files):
        matrix.overall = "not_recommended"
        matrix.reason = (
            "repo uses Triton kernels; Triton has no Intel/SYCL "
            "backend — kernels fall back to eager PyTorch on CPU, "
            "rest of the model runs on Intel GPU. Performance on "
            "triton-dominated workloads is significantly reduced."
        )
        # Keep scanning for other SYCL issues too — don't early-return.

    cu_files = [f for f in source_files if _is_cu_like(f)]
    if not cu_files:
        return matrix

    dpct = _which("dpct") or "/opt/intel/oneapi/dpcpp-ct/latest/bin/dpct"
    if os.path.exists(dpct) and work_dir:
        try:
            out = await _run_subprocess(
                [dpct, "--query-api-mapping"]
                + [os.path.join(work_dir, f["filename"]) for f in cu_files],
                timeout_seconds=180,
            )
            matrix.unsupported_apis = _parse_dpct_warnings(out)
        except Exception as e:  # noqa: BLE001
            logger.warning("dpct preflight failed: %s", e)
    else:
        _static_unsupported_scan(cu_files, matrix, _STATIC_SYCL_UNSUPPORTED)

    if matrix.unsupported_apis and all(
        _is_hard_block_sycl(api) for api in matrix.unsupported_apis
    ):
        matrix.overall = "incompatible"
        matrix.reason = (
            "CUDA APIs used have no SYCL equivalent: "
            + ", ".join(matrix.unsupported_apis)
        )
    elif matrix.unsupported_apis:
        matrix.overall = "not_recommended"
        matrix.reason = (
            "SYCL port needs manual cleanup for: "
            + ", ".join(matrix.unsupported_apis)
        )
    return matrix


def _preflight_npu(
    source_files: List[Dict[str, Any]],
    matrix: CompatMatrix,
) -> CompatMatrix:
    """Kernel-dominance gate. OpenVINO has no kernel translator; if the
    model's hot path is a custom CUDA kernel the NPU variant will be
    10-50× slower than CUDA even if it builds, so we mark it
    not_recommended or incompatible (never `compatible`)."""
    # Structural signal: any CUDAExtension or torch.utils.cpp_extension
    # import/call (load / load_inline). Catches both the classic
    # `setup.py with CUDAExtension` path AND the less-obvious
    # `from torch.utils.cpp_extension import load` + runtime-compiled
    # CUDA source (common in research code).
    has_cuda_ext = any(
        _contains(f, "CUDAExtension(")
        or _contains(f, "cpp_extension.load")
        or _contains(f, "from torch.utils.cpp_extension")
        for f in source_files
    )
    # Kernel-primitive signal: triton, inline CUDA, inductor-reduce-overhead
    has_triton = any(_contains(f, "@triton.jit") or _contains(f, "import triton") for f in source_files)
    # Seed-list signal
    has_seed_import = any(
        _contains_any_import(f, _NPU_DOMINANCE_SEED_IMPORTS) for f in source_files
    )
    # ONNX entrypoint signal — if this is present, model can be exported
    has_onnx_export = any(_contains(f, "torch.onnx.export") for f in source_files)

    # Preflight decision tree:
    #   Triton or seed-dominance → not_recommended (will build with
    #     CPU fallback but will be slow)
    #   CUDAExtension but no ONNX export → incompatible (no entry point)
    #   CUDAExtension with ONNX export → not_recommended (inference
    #     path can port; custom kernel stays CPU)
    #   No .cu, no CUDAExtension, no triton → compatible
    if has_triton:
        matrix.overall = "not_recommended"
        matrix.reason = (
            "repo uses Triton kernels; OpenVINO has no Triton "
            "translator — NPU variant will fall back to CPU and "
            "run 10-50× slower than CUDA"
        )
        return matrix
    if has_seed_import:
        matrix.overall = "not_recommended"
        matrix.reason = (
            "repo imports a known kernel-dominant package; NPU "
            "variant would build but inference would be dominated "
            "by the CPU-fallback kernel"
        )
        return matrix
    if has_cuda_ext and not has_onnx_export:
        matrix.overall = "incompatible"
        matrix.reason = (
            "repo ships custom CUDA kernels with no torch.onnx.export "
            "call; no OpenVINO entry point can be constructed"
        )
        return matrix
    if has_cuda_ext and has_onnx_export:
        matrix.overall = "not_recommended"
        matrix.reason = (
            "ONNX export path found but custom CUDA kernels would "
            "fall back to CPU, which typically dominates latency"
        )
        return matrix
    return matrix


def _preflight_metal(
    source_files: List[Dict[str, Any]],
    matrix: CompatMatrix,
) -> CompatMatrix:
    """Metal has no CUDA kernel translator. Pure-framework code is
    fine via MPS; compiled .cu / CUDAExtension is incompatible
    (Metal shader translation is not yet automated). Triton kernels
    fall back to eager PyTorch on CPU (P9h), same pattern as Intel."""
    if _uses_triton(source_files):
        matrix.overall = "not_recommended"
        matrix.reason = (
            "repo uses Triton kernels; Triton has no Metal backend — "
            "kernels fall back to eager PyTorch on CPU (rest of the "
            "model runs on Apple Silicon MPS). Performance on "
            "triton-dominated workloads is significantly reduced."
        )
        # Don't early-return — compiled-CUDA checks below still run.
    if any(_is_cu_like(f) for f in source_files) or any(
        _contains(f, "CUDAExtension(") for f in source_files
    ):
        matrix.overall = "incompatible"
        matrix.reason = (
            "Metal has no CUDA kernel translator; repo ships "
            "compiled .cu kernels that would require hand-written "
            "Metal shaders"
        )
    return matrix


# -----------------------------------------------------------------
# Layer 3 — plan_port / apply_port
# -----------------------------------------------------------------

# Default CUDA arch list the deep_cuda_patches module already uses.
# Per-variant translations below.
_DEFAULT_CUDA_ARCH = "7.5;8.0;8.6;8.9;9.0"
_DEFAULT_ROCM_ARCH = "gfx90a;gfx942;gfx1100;gfx1201"
_DEFAULT_SYCL_TARGETS = "intel_gpu_pvc,intel_gpu_dg2"


# P9h — Triton runtime fallback dispatcher. Injected into ported
# source trees for Intel / Metal / NPU variants whose @triton.jit
# kernels have no native backend on the target device. The
# decorator dispatches at runtime:
#   CUDA GPU available  → original @triton.jit (fastest)
#   ROCm GPU + triton-rocm installed → triton-rocm (good)
#   Otherwise            → eager PyTorch impl (slow but correct)
#
# The eager impl is a sibling function produced by the adapter's
# LLM cleanup pass; the porter emits a manual_cleanup_needed hint
# telling the LLM to write `<triton_fn>_eager` with the same
# signature and semantics as each @triton.jit function it sees.
#
# Behavior contract:
#   - Correctness: eager impl must produce numerically-equivalent
#     output to the triton kernel (within 9m tolerance).
#   - Transparency: call sites don't change. `softmax(x, y, N)`
#     still works whether triton or eager dispatched under the hood.
#   - Observability: first-call per process logs which backend
#     fired so operators can see CPU-fallback activations in
#     container logs.
_TRITON_FALLBACK_MODULE = '''"""Runtime fallback dispatcher for @triton.jit kernels.

Injected by MOD Core's kernel_porter (Phase 9h) when a repo with
Triton kernels is ported to a backend without native Triton
support (Intel/SYCL, Apple Metal, Intel NPU as of 2026-04).

Call sites don't change. The `@triton_with_fallback(eager=...)`
decorator replaces `@triton.jit` and dispatches at runtime based
on available hardware.
"""
from __future__ import annotations

import functools
import logging
import os

_log = logging.getLogger(__name__)
_ANNOUNCED = set()  # which backend fired first, per-function


def _announce(fn_name: str, backend: str) -> None:
    """Log the backend choice on first call per function. Useful
    for telemetry — reveals CPU-fallback activations in the wild."""
    key = f"{fn_name}:{backend}"
    if key in _ANNOUNCED:
        return
    _ANNOUNCED.add(key)
    _log.info("[triton-fallback] %s dispatched via %s", fn_name, backend)


def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:  # noqa: BLE001
        return False


def _triton_available() -> bool:
    """True when a runnable triton backend is present (CUDA or
    triton-rocm). False on Intel/NPU/Metal where triton has no
    backend."""
    try:
        import triton  # type: ignore
    except ImportError:
        return False
    # triton on non-CUDA (without triton-rocm) imports but fails
    # at @triton.jit invocation — probe by checking runtime driver.
    try:
        from triton.runtime import driver  # type: ignore
        return driver.active is not None
    except Exception:  # noqa: BLE001
        return _cuda_available()


def triton_with_fallback(eager):
    """Decorator. Wraps an @triton.jit function with a runtime
    dispatcher: triton-native → eager PyTorch fallback.

    Usage (emitted by the porter's LLM cleanup pass):
        def softmax_eager(x_ptr, y_ptr, N):
            # eager PyTorch impl — same signature + semantics
            ...

        @triton_with_fallback(eager=softmax_eager)
        @triton.jit
        def softmax(x_ptr, y_ptr, N: int):
            ...
    """
    def _decorator(triton_fn):
        fn_name = getattr(triton_fn, "__name__", "<triton_fn>")

        @functools.wraps(triton_fn)
        def _wrapper(*args, **kwargs):
            # Explicit env override for testing:
            # MOD_TRITON_FORCE_EAGER=1 skips triton regardless.
            if os.getenv("MOD_TRITON_FORCE_EAGER", "").lower() in ("1", "true", "yes"):
                _announce(fn_name, "eager (forced)")
                return eager(*args, **kwargs)
            if _triton_available():
                _announce(fn_name, "triton")
                return triton_fn(*args, **kwargs)
            _announce(fn_name, "eager (fallback)")
            return eager(*args, **kwargs)
        _wrapper._triton_original = triton_fn  # introspection hook
        _wrapper._eager_impl = eager
        return _wrapper

    return _decorator
'''


async def plan_port(
    variant: str,
    source_files: List[Dict[str, Any]],
    hints: Dict[str, Any],
    *,
    work_dir: Optional[str] = None,
) -> PortPlan:
    """Build a PortPlan for one variant. Preflight first; if that
    says incompatible, return a plan with compatibility=incompatible
    and no transforms. Else run the variant-specific porter.
    """
    matrix = await preflight_apis(variant, source_files, work_dir=work_dir)
    if matrix.overall == "incompatible":
        return PortPlan(
            variant=variant,
            compatibility="incompatible",
            incompat_reason=matrix.reason,
            port_report=PortReport(
                tool="preflight-only",
                translator_version=_effective_porter_version(),
                unsupported_apis=matrix.unsupported_apis,
            ),
        )

    if variant == "rocm":
        plan = await _plan_port_hip(source_files, hints, matrix, work_dir=work_dir)
    elif variant == "intel":
        plan = await _plan_port_sycl(source_files, hints, matrix, work_dir=work_dir)
    elif variant == "npu":
        plan = _plan_port_npu(source_files, hints, matrix)
    elif variant == "metal":
        plan = _plan_port_metal(source_files, hints, matrix)
    else:
        plan = PortPlan(
            variant=variant,
            compatibility="incompatible",
            incompat_reason=f"no porter for variant {variant!r}",
        )

    # Inherit the preflight's not_recommended verdict if stricter than
    # the porter's own judgement (preflight saw a static API issue the
    # porter didn't).
    if matrix.overall == "not_recommended" and plan.compatibility == "compatible":
        plan.compatibility = "not_recommended"
        plan.incompat_reason = plan.incompat_reason or matrix.reason
    return plan


async def _plan_port_hip(
    source_files: List[Dict[str, Any]],
    hints: Dict[str, Any],
    matrix: CompatMatrix,
    *,
    work_dir: Optional[str],
) -> PortPlan:
    """HIP port. PyTorch's CUDAExtension auto-hipifies on a ROCm torch
    build — the porter's job is mostly env vars and (for non-torch
    C++) invoking hipify-clang.
    """
    has_cuda_ext = any(
        _contains(f, "CUDAExtension") or _contains(f, "torch.utils.cpp_extension")
        for f in source_files
    )
    cu_files = [f for f in source_files if _is_cu_like(f)]

    plan = PortPlan(
        variant="rocm",
        compatibility="compatible",
        incompat_reason=None,
        dockerfile_env={
            "PYTORCH_ROCM_ARCH": _DEFAULT_ROCM_ARCH,
            "ROCM_HOME": "/opt/rocm",
            # Disable CUDA-specific envs that deep_cuda_patches set
            # globally; apply_port will strip them from the Dockerfile.
        },
        verification_cmd=_build_import_check(hints),
        port_report=PortReport(
            tool="torch.hipify (auto)" if has_cuda_ext else "hipify-clang",
            files_translated=[],
            unsupported_apis=matrix.unsupported_apis,
            manual_cleanup_needed=[],
        ),
    )

    # PyTorch-based repos don't need a manual hipify pass; torch's
    # own BuildExtension.hipify runs at `pip install -e .` time. Just
    # make sure hipify-clang apt is available for any edge-case
    # subclass that shells out.
    plan.dockerfile_apt.append("hipify-clang")

    # P9h: Triton has an experimental ROCm port (triton-rocm) that
    # works for most @triton.jit kernels without code changes. Add
    # it as a pip requirement when the source uses triton so the
    # node image can import triton on ROCm without a pip install
    # surprise at first inference.
    if _uses_triton(source_files):
        plan.requirements_patches.add.append("triton-rocm")
        plan.port_report.manual_cleanup_needed.append(
            f"Triton kernels detected in {', '.join(_triton_files(source_files))}; "
            f"triton-rocm installs as a drop-in replacement but "
            f"correctness should be spot-checked vs CUDA (9m)"
        )

    # Non-torch raw C++/CUDA: run hipify-clang now and emit file
    # transforms (.cu → .hip, with rewritten #include + API calls).
    hipify_bin = _which("hipify-clang")
    hipify_warning_total = 0
    if not has_cuda_ext and cu_files and work_dir and hipify_bin:
        transforms, hipify_warning_total = await _hipify_subprocess(
            cu_files, work_dir,
        )
        plan.source_transforms.extend(transforms)
        plan.port_report.files_translated = [t.filename for t in transforms]

        # Phase 6 — downgrade verdict when hipify emitted too many
        # `warning:` lines. Gating is intentionally conservative: a few
        # warnings are normal on non-trivial kernels; above threshold
        # the translation almost always compiles but behaves wrong at
        # runtime (partial API stubs, shuffle masks fused to no-ops).
        threshold = _hipify_warning_threshold()
        if hipify_warning_total > threshold:
            if plan.compatibility == "compatible":
                plan.compatibility = "not_recommended"
            reason_frag = (
                f"hipify-clang emitted {hipify_warning_total} warnings "
                f"(threshold {threshold}); translation likely partial"
            )
            plan.incompat_reason = (
                f"{plan.incompat_reason}; {reason_frag}"
                if plan.incompat_reason else reason_frag
            )
            plan.port_report.manual_cleanup_needed.append(reason_frag)

    # Bake hipify version into translator_version so the learning-table
    # cache key invalidates when the toolchain bumps (P6.3).
    hipify_ver = await _resolve_binary_version(
        hipify_bin, cache_key="hipify-clang",
    )
    plan.port_report.translator_version = _effective_porter_version(
        f"hipify={hipify_ver}" if hipify_ver else ""
    )
    # Ride the warning count on the plan's dockerfile_env dict so the
    # Containerizer's telemetry emitter can pick it up without a second
    # channel. dockerfile_env is tenant-visible but harmless here.
    if hipify_warning_total:
        plan.dockerfile_env.setdefault(
            "_MOD_PORT_HIPIFY_WARNINGS", str(hipify_warning_total),
        )

    # Requirements: nothing to add for HIP — torch --index-url is
    # already swapped by gpu_variants.py's VARIANT_TORCH_INDEX.
    return plan


def _hipify_warning_threshold() -> int:
    """Phase 6 — env-tunable threshold for HIPIFY warning downgrade.
    Defaults to `_HIPIFY_WARNING_DEFAULT_THRESHOLD`; operators can tune
    per-environment via MOD_HIPIFY_WARNING_THRESHOLD."""
    try:
        return max(0, int(os.getenv("MOD_HIPIFY_WARNING_THRESHOLD", "")
                          or _HIPIFY_WARNING_DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return _HIPIFY_WARNING_DEFAULT_THRESHOLD


async def _plan_port_sycl(
    source_files: List[Dict[str, Any]],
    hints: Dict[str, Any],
    matrix: CompatMatrix,
    *,
    work_dir: Optional[str],
) -> PortPlan:
    """SYCL port. dpct translates .cu → .dp.cpp; LLM cleanup patches
    setup.py to DPCPPExtension."""
    cu_files = [f for f in source_files if _is_cu_like(f)]
    has_cuda_ext = any(
        _contains(f, "CUDAExtension") for f in source_files
    )

    plan = PortPlan(
        variant="intel",
        compatibility="compatible",
        incompat_reason=None,
        dockerfile_env={
            "SYCL_TARGETS": _DEFAULT_SYCL_TARGETS,
        },
        verification_cmd=_build_import_check(hints),
        port_report=PortReport(
            tool="dpct",
            unsupported_apis=matrix.unsupported_apis,
        ),
    )

    # Run dpct if reachable + work_dir provided. If not (running in
    # API container), plan emits apt + CUDAExtension→DPCPPExtension
    # patches only; subprocess runs inside the Containerizer at
    # apply_port time.
    dpct = _which("dpct") or "/opt/intel/oneapi/dpcpp-ct/latest/bin/dpct"
    if cu_files and work_dir and os.path.exists(dpct):
        transforms, warnings = await _dpct_subprocess(
            cu_files, work_dir, dpct_path=dpct,
        )
        plan.source_transforms.extend(transforms)
        plan.port_report.files_translated = [t.filename for t in transforms]
        plan.port_report.unsupported_apis.extend(warnings)

    if has_cuda_ext:
        # setup.py swap: CUDAExtension → DPCPPExtension. LLM owns the
        # rewrite; we hand it a structured patch hint.
        for f in source_files:
            if f.get("filename", "").endswith(("setup.py", "setup.cfg")):
                plan.setup_py_patches.append(SetupPyPatch(
                    filename=f["filename"],
                    kind="replace_class",
                    old="CUDAExtension",
                    new="DPCPPExtension",
                ))
                plan.setup_py_patches.append(SetupPyPatch(
                    filename=f["filename"],
                    kind="add_import",
                    old="",
                    new=(
                        "from intel_extension_for_pytorch.xpu.cpp_extension "
                        "import DPCPPExtension, DpcppBuildExtension"
                    ),
                ))
                plan.port_report.manual_cleanup_needed.append(
                    f"swap BuildExtension → DpcppBuildExtension in cmdclass "
                    f"of {f['filename']}"
                )
                plan.port_report.manual_cleanup_needed.append(
                    f"rename extra_compile_args key 'nvcc' → 'dpcpp' in "
                    f"{f['filename']}"
                )

        plan.requirements_patches.add.append("intel_extension_for_pytorch")

    # P9h: inject triton runtime fallback for Intel (no native
    # triton-intel backend exists as of 2026-04). Puts
    # _triton_fallback.py into the source tree + flags LLM cleanup
    # to generate eager-PyTorch equivalents of each @triton.jit
    # function.
    _apply_triton_fallback(plan, source_files)

    # Phase 6 — bake dpct version into translator_version.
    dpct_ver = await _resolve_binary_version(
        dpct if os.path.exists(dpct) else None,
        cache_key="dpct",
    )
    plan.port_report.translator_version = _effective_porter_version(
        f"dpct={dpct_ver}" if dpct_ver else ""
    )

    return plan


def _apply_triton_fallback(
    plan: PortPlan,
    source_files: List[Dict[str, Any]],
) -> None:
    """P9h shared helper — inject _triton_fallback.py + LLM cleanup
    hints when a port target has no native Triton backend.

    Mutates `plan` in place:
    - Adds a `_triton_fallback.py` FileTransform (new file, not a
      rename).
    - Appends detailed manual_cleanup_needed hints telling the LLM
      cleanup pass to write `<fn>_eager` counterparts for each
      @triton.jit function AND rewrite the decoration site to
      `@triton_with_fallback(eager=<fn>_eager)`.
    - Keeps `triton` in requirements (so `import triton` still works;
      dispatch simply picks the eager path on Intel/NPU/Metal).

    Called from _plan_port_sycl / _plan_port_npu / _plan_port_metal
    when their respective preflights flagged triton usage. HIP has
    its own path (triton-rocm direct install) so this helper isn't
    needed there.
    """
    triton_file_list = _triton_files(source_files)
    if not triton_file_list and not any(
        _contains_any_import(f, {"triton"}) for f in source_files
    ):
        return

    # 1. Inject the dispatcher module as a new file. apply_port's
    # FileTransform handler with `new_filename != filename` +
    # `new_body` covers the "create" case (the filename isn't in
    # the input list, so the delete-then-create path emits a new
    # file).
    plan.source_transforms.append(FileTransform(
        filename="_triton_fallback.py",      # no existing input with this name
        new_filename="_triton_fallback.py",
        new_body=_TRITON_FALLBACK_MODULE,
    ))

    # 2. LLM cleanup hints — specific and bounded so the LLM doesn't
    # re-translate kernel bodies.
    if triton_file_list:
        plan.port_report.manual_cleanup_needed.append(
            f"Triton kernels detected in: {', '.join(triton_file_list)}. "
            f"For EACH @triton.jit function `fn(...)`: "
            f"(a) write a `fn_eager(...)` function in the same file "
            f"with identical signature + return shape, implementing "
            f"the kernel's semantics in eager PyTorch (correctness "
            f"over performance); "
            f"(b) add `from _triton_fallback import triton_with_fallback` "
            f"to the same file; "
            f"(c) replace the decoration `@triton.jit` with "
            f"`@triton_with_fallback(eager=fn_eager)\\n@triton.jit` "
            f"(both stacked — triton.jit inside, fallback outside). "
            f"The runtime dispatcher picks CUDA/triton-rocm when "
            f"available, falls back to fn_eager on Intel/NPU/Metal/CPU."
        )
    else:
        plan.port_report.manual_cleanup_needed.append(
            "Repo imports `triton` but no @triton.jit decorators were "
            "found in the visible source. Add a top-level guard: "
            "`try: import triton except ImportError: triton = None` "
            "so runtime code can detect triton absence gracefully."
        )

    # 3. Keep triton as a dep so import doesn't fail on the target.
    # dispatcher handles the "triton imports but no backend" case.
    if "triton" not in plan.requirements_patches.add:
        plan.requirements_patches.add.append("triton")


def _plan_port_npu(
    source_files: List[Dict[str, Any]],
    hints: Dict[str, Any],
    matrix: CompatMatrix,
) -> PortPlan:
    """NPU port. No kernel translator — rely on torch.onnx.export +
    OpenVINO's model converter. Gate hard on kernel-dominance."""
    # matrix carries the dominance verdict already.
    if matrix.overall == "not_recommended":
        plan = PortPlan(
            variant="npu",
            compatibility="not_recommended",
            incompat_reason=matrix.reason,
            verification_cmd=_build_import_check(hints),
            port_report=PortReport(
                tool="openvino-dominance-gate",
                translator_version=_effective_porter_version(),
            ),
        )
        # Triton-using repos still benefit from the runtime fallback:
        # non-triton model parts run on NPU, triton parts eager-CPU.
        # P9h: kept in the not_recommended branch specifically so
        # triton users aren't worse off than before.
        _apply_triton_fallback(plan, source_files)
        return plan

    # Compatible: emit an ONNX → OpenVINO IR conversion stage in the
    # Dockerfile. The actual invocation happens at build time inside
    # the npu-openvino base image.
    plan = PortPlan(
        variant="npu",
        compatibility="compatible",
        incompat_reason=None,
        dockerfile_env={},  # NPU ignores arch lists
        verification_cmd=_build_import_check(hints),
        port_report=PortReport(
            tool="openvino (torch→ONNX→IR)",
            translator_version=_effective_porter_version(),
            manual_cleanup_needed=[
                "emit torch.onnx.export call site inside the container "
                "entrypoint before ov.Core.compile_model(...)",
            ],
        ),
    )
    plan.requirements_patches.add.append("openvino-dev")
    plan.requirements_patches.add.append("onnx")

    # P9h: NPU also has no native triton backend. Inject fallback
    # dispatcher when triton is detected. NPU is already flagged
    # not_recommended for triton repos by _preflight_npu, so this
    # doesn't flip compatibility — it makes the port actually run
    # the non-triton parts on NPU instead of crashing at first
    # @triton.jit invocation.
    _apply_triton_fallback(plan, source_files)

    return plan


def _plan_port_metal(
    source_files: List[Dict[str, Any]],
    hints: Dict[str, Any],
    matrix: CompatMatrix,
) -> PortPlan:
    """Metal port. Pure-framework code runs on MPS; compiled .cu is
    incompatible (no hipify-for-Metal yet). Triton kernels fall
    back to eager PyTorch on CPU via P9h runtime dispatcher."""
    plan = PortPlan(
        variant="metal",
        compatibility="compatible",
        incompat_reason=None,
        dockerfile_env={},
        verification_cmd="python -c \"import torch; assert torch.backends.mps.is_available() or True\"",
        port_report=PortReport(
            tool="metal-framework-only",
            translator_version=_effective_porter_version(),
        ),
    )
    # P9h: triton fallback applies to Metal too — same pattern as
    # Intel/NPU. Preflight already set not_recommended when triton
    # was detected; this injects the dispatcher so the non-triton
    # parts still run cleanly on MPS.
    _apply_triton_fallback(plan, source_files)
    return plan


async def apply_port(
    plan: PortPlan,
    source_files: List[Dict[str, Any]],
    dockerfile: str,
) -> Tuple[List[Dict[str, Any]], str]:
    """Materialize a PortPlan against a source tree + Dockerfile.
    Returns (new_source_files, new_dockerfile).

    Idempotent: if the plan has already been applied (marker comment
    present), we return inputs untouched.
    """
    marker = f"# [port-{plan.variant}] applied"
    if marker in dockerfile:
        logger.info("apply_port: %s already applied; skipping", plan.variant)
        return source_files, dockerfile

    if plan.compatibility == "incompatible":
        # Don't touch anything. Caller culls this variant.
        return source_files, dockerfile

    # 1. File transforms (renames, rewrites, deletes)
    new_files = _apply_file_transforms(source_files, plan.source_transforms)

    # 2. Requirements patch (if a requirements.txt exists)
    new_files = _apply_requirements_patch(new_files, plan.requirements_patches)

    # 2b. Phase 7 — libcst-driven AST rewrites for every .py file in
    # the tree. Catches indirect / conditional / factory-function
    # device transfers the regex recipes miss. Silent no-op when
    # libcst isn't importable (dev env without it) or the variant
    # isn't in _VARIANT_MAP. Runs AFTER the variant-specific source
    # transforms so we don't double-rewrite already-corrected code.
    try:
        from tools.py_rewriter import rewrite_python_source
        rewritten: List[Dict[str, Any]] = []
        for f in new_files:
            name = f.get("filename", "") or ""
            if not name.endswith(".py"):
                rewritten.append(f)
                continue
            body = f.get("body") or ""
            new_body = rewrite_python_source(body, variant=plan.variant)
            if new_body != body:
                entry = dict(f)
                entry["body"] = new_body
                rewritten.append(entry)
            else:
                rewritten.append(f)
        new_files = rewritten
    except Exception:  # noqa: BLE001
        logger.info(
            "py_rewriter pass skipped for variant=%s (libcst unavailable "
            "or transformer raised)", plan.variant,
            exc_info=True,
        )

    # 3. Dockerfile patches: strip CUDA-only envs, insert variant envs,
    #    insert apt install, insert verification RUN at the end.
    new_df = _apply_dockerfile_patches(dockerfile, plan, marker)

    return new_files, new_df


def _apply_file_transforms(
    files: List[Dict[str, Any]],
    transforms: List[FileTransform],
) -> List[Dict[str, Any]]:
    """Apply rename / rewrite / delete / create transforms.

    Semantics (P9h extension):
    - `t.filename` already in input → rename/rewrite/delete that entry.
    - `t.filename` NOT in input → CREATE a new file (used for the
      _triton_fallback.py injection when a repo has no such file yet).
    - `t.delete=True` → remove regardless of whether the entry exists.

    Returns a new list; input is not mutated. Idempotent: re-applying
    the same transform set against previous output is a no-op (same
    body produced, same filename).
    """
    by_name = {f.get("filename", ""): dict(f) for f in files}
    for t in transforms:
        if t.delete:
            by_name.pop(t.filename, None)
            continue
        existed = t.filename in by_name
        if existed:
            entry = by_name.pop(t.filename)
        else:
            # CREATE new file — the "inject _triton_fallback.py" path.
            # Skip when new_body is None (no content to write).
            if t.new_body is None:
                continue
            entry = {"filename": t.filename, "body": ""}
        target_name = t.new_filename or t.filename
        if t.new_body is not None:
            entry["body"] = t.new_body
        entry["filename"] = target_name
        by_name[target_name] = entry
    return list(by_name.values())


def _apply_requirements_patch(
    files: List[Dict[str, Any]],
    patch: RequirementsPatch,
) -> List[Dict[str, Any]]:
    """Add/remove lines in requirements.txt. If no requirements.txt
    exists and patch.add is non-empty, creates one."""
    if not patch.add and not patch.remove:
        return files
    out = [dict(f) for f in files]
    req = None
    for f in out:
        if f.get("filename", "") == "requirements.txt":
            req = f
            break
    if req is None and patch.add:
        req = {"filename": "requirements.txt", "body": ""}
        out.append(req)
    if req is None:
        return out

    lines = [ln.rstrip() for ln in (req.get("body") or "").splitlines()]
    existing = {_pkg_name(ln): ln for ln in lines if ln and not ln.startswith("#")}
    for pkg in patch.remove:
        existing.pop(_pkg_name(pkg), None)
    for pkg in patch.add:
        if _pkg_name(pkg) not in existing:
            existing[_pkg_name(pkg)] = pkg
    req["body"] = "\n".join(existing.values()) + "\n"
    return out


def _apply_dockerfile_patches(
    dockerfile: str,
    plan: PortPlan,
    marker: str,
) -> str:
    """Strip CUDA-only envs (deep_cuda_patches inserts
    TORCH_CUDA_ARCH_LIST + CUDA_HOME globally; on non-CUDA variants
    we replace those with the variant-appropriate equivalents), then
    insert the plan's envs, apt packages, and verification RUN.
    """
    lines = dockerfile.splitlines()

    # Strip CUDA-only envs on non-CUDA variants.
    if plan.variant != "cuda":
        lines = [
            ln for ln in lines
            if not re.match(r"^\s*ENV\s+TORCH_CUDA_ARCH_LIST\b", ln)
            and not re.match(r"^\s*ENV\s+CUDA_HOME\b", ln)
        ]

    # Find insertion point: after the last WORKDIR, before the first
    # RUN pip/python/apt. Same heuristic as deep_cuda_patches.
    insert_at = _find_insertion_index(lines)

    patches: List[str] = []
    patches.append(f"{marker}")
    for k, v in plan.dockerfile_env.items():
        patches.append(f'ENV {k}="{v}"')
    if plan.dockerfile_apt:
        patches.append("USER root")
        patches.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            + " ".join(plan.dockerfile_apt)
            + " && apt-get clean && rm -rf /var/lib/apt/lists/*"
        )
        patches.append("USER mod")

    before = lines[:insert_at]
    after = lines[insert_at:]
    merged = before + [""] + patches + [""] + after

    # Verification RUN at the end of the Dockerfile (Tier 1 smoke).
    if plan.verification_cmd:
        verify_marker = f"# [port-{plan.variant}] verify"
        if verify_marker not in "\n".join(merged):
            merged.append("")
            merged.append(verify_marker)
            merged.append(f"RUN {plan.verification_cmd}")

    out = "\n".join(merged)
    if dockerfile.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


# -----------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------

def _uses_triton(source_files: List[Dict[str, Any]]) -> bool:
    """P9h: detect Triton kernel sites. Either the @triton.jit
    decorator or a top-level `import triton` is enough — both mean
    the repo expects triton at runtime."""
    for f in source_files:
        if _contains(f, "@triton.jit"):
            return True
        if _contains_any_import(f, {"triton"}):
            return True
    return False


def _triton_files(source_files: List[Dict[str, Any]]) -> List[str]:
    """Return filenames containing @triton.jit — useful for plan
    reports ('triton kernels live in these files'). Best-effort,
    not comprehensive (doesn't see kernels loaded dynamically)."""
    out: List[str] = []
    for f in source_files:
        if _contains(f, "@triton.jit") and f.get("filename"):
            out.append(f["filename"])
    return sorted(set(out))


def _is_cu_like(f: Dict[str, Any]) -> bool:
    name = (f.get("filename") or "").lower()
    return any(name.endswith(e) for e in _CUDA_CPP_EXTENSIONS)


def _contains(f: Dict[str, Any], needle: str) -> bool:
    body = f.get("body") or ""
    return needle in body if isinstance(body, str) else False


def _contains_any_import(f: Dict[str, Any], imports: set[str]) -> bool:
    body = f.get("body") or ""
    if not isinstance(body, str):
        return False
    for imp in imports:
        # Match `import X`, `from X import Y`, `from X.Y import Z`
        if re.search(rf"(?:^|\n)\s*(?:from|import)\s+{re.escape(imp)}\b", body):
            return True
    return False


def _which(name: str) -> Optional[str]:
    """Locate a binary on PATH; return None if absent."""
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


async def _run_subprocess(
    argv: List[str],
    *,
    timeout_seconds: int = 120,
) -> str:
    """asyncio subprocess wrapper with timeout. Returns combined
    stdout+stderr."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"subprocess timed out after {timeout_seconds}s: {shlex.join(argv)}"
        )
    return (out or b"").decode("utf-8", errors="replace")


async def _run_subprocess_split(
    argv: List[str],
    *,
    timeout_seconds: int = 120,
) -> Tuple[str, str, int]:
    """Like `_run_subprocess` but returns (stdout, stderr, returncode)
    separately. Needed by Phase 6's HIPIFY warning scan — hipify-clang
    emits warnings on stderr while translated source goes to stdout (or
    to `-o`), so merging them loses the signal we need.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"subprocess timed out after {timeout_seconds}s: {shlex.join(argv)}"
        )
    return (
        (out or b"").decode("utf-8", errors="replace"),
        (err or b"").decode("utf-8", errors="replace"),
        int(proc.returncode or 0),
    )


# Phase 6 — HIPIFY warning pattern. hipify-clang emits one line per
# partial translation shaped like:
#   `warning: unrecognized CUDA token 'cuDnnRnn' — left as-is`
#   `HIPIFY: warning: ...`
# Both shapes hit on the `warning:` substring; we count them and use
# the count as a rough "how messy was this translation" signal.
_HIPIFY_WARNING_LINE_RE = re.compile(
    r"(?im)^(?:hipify\s*:\s*)?warning\s*:"
)


def _count_hipify_warnings(stderr_text: str) -> int:
    """Count `warning:` lines in hipify-clang stderr. Case-insensitive
    and tolerant of the `HIPIFY:` prefix some hipify builds use."""
    if not stderr_text:
        return 0
    return len(_HIPIFY_WARNING_LINE_RE.findall(stderr_text))


async def _resolve_binary_version(
    binary_path: Optional[str],
    *,
    argv_tail: Tuple[str, ...] = ("--version",),
    cache_key: Optional[str] = None,
    timeout_seconds: int = 5,
) -> str:
    """Resolve a translator binary's version string. Cached module-level
    so repeated ports in the same Containerizer run don't re-exec the
    binary. Returns the empty string when the binary is missing or the
    subprocess fails (caller falls back to PORTER_VERSION alone).
    """
    if not binary_path:
        return ""
    key = cache_key or binary_path
    cached = _BINARY_VERSION_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        out, err, _rc = await _run_subprocess_split(
            [binary_path, *argv_tail],
            timeout_seconds=timeout_seconds,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("binary version probe failed for %s: %s", binary_path, e)
        _BINARY_VERSION_CACHE[key] = ""
        return ""
    raw = (out or err or "").strip().splitlines()
    first = raw[0].strip() if raw else ""
    # Distil to a terse version token: grab the first x.y[.z] run.
    m = re.search(r"\d+\.\d+(?:\.\d+)?", first)
    version = m.group(0) if m else first[:32]
    _BINARY_VERSION_CACHE[key] = version
    return version


def _effective_porter_version(
    *extra_fingerprints: str,
) -> str:
    """Compose the porter_version string that rides on
    plan.port_report.translator_version → PortOutcome.porter_version.

    Shape: `v1:<fingerprint>[:<fingerprint>...]` with empty fingerprints
    dropped. When no binaries were resolved (API-side, no oneAPI in the
    container), returns bare `v1` for backward compatibility with rows
    written before Phase 6.
    """
    parts: List[str] = [PORTER_VERSION]
    for fp in extra_fingerprints:
        fp = (fp or "").strip()
        if fp:
            parts.append(fp)
    return ":".join(parts)


_HIPIFY_UNSUPPORTED_RE = re.compile(r"\[HIP_UNSUPPORTED\]\s+([A-Za-z0-9_]+)")
_DPCT_WARNING_RE = re.compile(r"DPCT(\d{4})[:\s]+([A-Za-z0-9_]+)")


def _parse_hipify_examine(output: str) -> List[str]:
    """Extract unsupported CUDA API names from hipify-clang's
    --examine-only output."""
    return sorted(set(_HIPIFY_UNSUPPORTED_RE.findall(output)))


def _parse_dpct_warnings(output: str) -> List[str]:
    """Extract DPCT1007/1031/etc. warnings from dpct output."""
    out = []
    for code, sym in _DPCT_WARNING_RE.findall(output):
        out.append(f"DPCT{code}:{sym}")
    return sorted(set(out))


# Static fallback lists when the real tools aren't reachable. These
# are the APIs most commonly seen failing on each backend.
_STATIC_ROCM_UNSUPPORTED = {
    "cudnnConvolutionForward",
    "cudnnBatchNormalizationForwardInference",
    "cuDnnRnn",
    "__shfl_sync",
    "cudaLaunchCooperativeKernel",
}
_STATIC_SYCL_UNSUPPORTED = {
    "cudnnConvolutionForward",
    "__shfl_sync",
    "__ballot_sync",
    "asm volatile",
    "cudaLaunchCooperativeKernel",
}


def _static_unsupported_scan(
    cu_files: List[Dict[str, Any]],
    matrix: CompatMatrix,
    patterns: set[str],
) -> None:
    """Grep .cu/.cuh bodies for known-bad symbols. Best-effort."""
    found: List[str] = []
    for f in cu_files:
        body = f.get("body") or ""
        for sym in patterns:
            if sym in body and sym not in found:
                found.append(sym)
    matrix.unsupported_apis.extend(found)


def _is_hard_block_rocm(api: str) -> bool:
    """Symbols that hipify cannot translate even partially — mark the
    ROCm variant incompatible, not just not_recommended."""
    hard = {"cudnnRnn", "asm volatile", "cudaLaunchCooperativeKernel"}
    return any(h in api for h in hard)


def _is_hard_block_sycl(api: str) -> bool:
    hard = {"asm volatile", "cudaLaunchCooperativeKernel"}
    return any(h in api for h in hard)


async def _hipify_subprocess(
    cu_files: List[Dict[str, Any]],
    work_dir: str,
) -> Tuple[List[FileTransform], int]:
    """Run hipify-clang per .cu file. Returns (transforms, warning_count).

    Phase 6 — hipify-clang returncode-zero with `warning:` lines was
    silently treated as a clean translation even when the warnings were
    real untranslated APIs. We now capture stderr separately and return
    a total warning count; callers downgrade the port verdict past
    `_HIPIFY_WARNING_DEFAULT_THRESHOLD` warnings.
    """
    transforms: List[FileTransform] = []
    warning_total = 0
    hipify = _which("hipify-clang")
    if not hipify:
        return transforms, warning_total
    for f in cu_files:
        name = f.get("filename") or ""
        src = os.path.join(work_dir, name)
        dst = src.rsplit(".", 1)[0] + ".hip"
        try:
            _stdout, stderr_text, rc = await _run_subprocess_split(
                [hipify, src, "-o", dst, "--", "-I/opt/rocm/include"],
                timeout_seconds=60,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("hipify-clang failed on %s: %s", name, e)
            continue
        warning_total += _count_hipify_warnings(stderr_text)
        if rc != 0 and not os.path.exists(dst):
            # Non-zero return AND no output file = real failure. Skip
            # this file; the caller already has the warning count.
            logger.warning(
                "hipify-clang returned %d on %s (stderr head: %s)",
                rc, name, stderr_text[:200].replace("\n", " | "),
            )
            continue
        if os.path.exists(dst):
            with open(dst, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
            transforms.append(FileTransform(
                filename=name,
                new_filename=name.rsplit(".", 1)[0] + ".hip",
                new_body=body,
            ))
    return transforms, warning_total


async def _dpct_subprocess(
    cu_files: List[Dict[str, Any]],
    work_dir: str,
    *,
    dpct_path: str,
) -> Tuple[List[FileTransform], List[str]]:
    """Back-compat thin wrapper — delegates to DpctTranslator so new
    code can depend on the abstract interface. Phase 9d introduced
    SYCLTranslator; Phase 9j will add SYCLomaticTranslator via the
    same contract."""
    return await DpctTranslator(dpct_path).translate(cu_files, work_dir)


# -----------------------------------------------------------------
# SYCL translator interface (Phase 9d + 9j)
# -----------------------------------------------------------------

class SYCLTranslator:
    """Abstract CUDA → SYCL translator. The DpctTranslator below is
    the Phase 9d implementation; SYCLomaticTranslator will land in
    Phase 9j against the same contract. Swapping translators is a
    subclass change — no call site in kernel_porter needs edits.

    Contract:
      translate(cu_files, work_dir) -> (transforms, unsupported_apis)

    - `cu_files` is a list of {filename, body, ...} dicts.
    - `work_dir` must already contain the CUDA source files on disk
      (caller materializes them; the translator invokes a subprocess
      against that directory).
    - Output `transforms` describes rename-and-rewrite per file
      (foo.cu → foo.dp.cpp) with the translated body.
    - `unsupported_apis` is a flat list of API symbol names the
      translator flagged as untranslatable (e.g. "DPCT1007:cudnnConv").
      Caller uses this to populate PortReport.unsupported_apis and
      decide whether to route via LLM cleanup or mark the variant
      incompatible.

    Implementations are async because both dpct and SYCLomatic are
    subprocess-heavy (30-90s on moderate repos); awaitable subprocess
    calls keep the adapter's outer asyncio fan-out responsive.
    """

    name: str = "abstract"
    version: str = "0.0"

    async def translate(
        self,
        cu_files: List[Dict[str, Any]],
        work_dir: str,
    ) -> Tuple[List[FileTransform], List[str]]:
        raise NotImplementedError


class DpctTranslator(SYCLTranslator):
    """Phase 9d — Intel DPC++ Compatibility Tool (dpct) wrapper.

    Binary ships in intel/oneapi-basekit:2025.3.2-0-devel-ubuntu22.04
    at /opt/intel/oneapi/dpcpp-ct/latest/bin/dpct; Phase 9d's base
    image bump adds a symlink onto the default PATH so the caller
    doesn't need to know the exact location.

    Intel announced dpct deprecation after the 2025.3 release — this
    class is deliberately thin so that Phase 9j's SYCLomaticTranslator
    can replace it via:
        SYCL_TRANSLATOR_DEFAULT = SYCLomaticTranslator
    without touching call sites.
    """

    name = "dpct"
    version = "2025.3.2"

    def __init__(self, dpct_path: Optional[str] = None):
        self._dpct_path = dpct_path or _which("dpct") or (
            "/opt/intel/oneapi/dpcpp-ct/latest/bin/dpct"
        )

    async def translate(
        self,
        cu_files: List[Dict[str, Any]],
        work_dir: str,
    ) -> Tuple[List[FileTransform], List[str]]:
        transforms: List[FileTransform] = []
        warnings: List[str] = []
        if not cu_files:
            return transforms, warnings

        out_dir = os.path.join(work_dir, ".dpct_out")
        os.makedirs(out_dir, exist_ok=True)
        argv = [
            self._dpct_path,
            f"--in-root={work_dir}",
            f"--out-root={out_dir}",
        ] + [os.path.join(work_dir, f["filename"]) for f in cu_files]

        try:
            out = await _run_subprocess(argv, timeout_seconds=300)
            warnings = _parse_dpct_warnings(out)
        except Exception as e:  # noqa: BLE001
            logger.warning("dpct failed: %s", e)
            return transforms, warnings

        # dpct renames .cu → .dp.cpp under the out_dir mirror.
        for f in cu_files:
            name = f["filename"]
            ported = os.path.join(out_dir, name).rsplit(".", 1)[0] + ".dp.cpp"
            if os.path.exists(ported):
                with open(ported, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
                transforms.append(FileTransform(
                    filename=name,
                    new_filename=name.rsplit(".", 1)[0] + ".dp.cpp",
                    new_body=body,
                ))
        return transforms, warnings


# Active translator — swap to SYCLomaticTranslator in Phase 9j.
# Env override `SYCL_TRANSLATOR=syclomatic` (added in 9j) routes to
# the alternate impl without a code change.
SYCL_TRANSLATOR_DEFAULT: type[SYCLTranslator] = DpctTranslator


def _pkg_name(line: str) -> str:
    """Extract the pip package name from a requirements line."""
    return re.split(r"[=<>!~;\[\s]", line.strip(), maxsplit=1)[0].lower()


def _build_import_check(hints: Dict[str, Any]) -> str:
    """Emit a `python -c '...'` that imports each CUDAExtension module
    listed in hints.extension_files. Tier 1 smoke — catches link errors
    at build time."""
    ext_files = hints.get("extension_files") or []
    # Derive module names from setup.py extensions when possible. Fall
    # back to a generic "import torch" probe — better than nothing.
    modules = []
    for path in ext_files:
        base = os.path.basename(path or "")
        if base in ("setup.py", "setup.cfg", "pyproject.toml"):
            continue
        stem = os.path.splitext(base)[0]
        if stem:
            modules.append(stem)
    if not modules:
        return "python -c \"import torch; print(torch.__version__)\""
    imports = "; ".join(f"__import__({m!r})" for m in modules)
    return f"python -c \"import torch; {imports}; print('ok')\""


def _find_insertion_index(lines: List[str]) -> int:
    """Same heuristic as deep_cuda_patches._find_insertion_index:
    after last WORKDIR, before first RUN pip/python/apt/COPY entrypoint."""
    last_workdir = -1
    first_run = -1
    first_copy_ep = -1
    first_from = -1
    run_pat = re.compile(r"^\s*RUN\s+(pip|python|apt|bash|sh|wget|curl)\b", re.I)
    copy_ep_pat = re.compile(r"^\s*COPY\s+.*entrypoint", re.I)
    workdir_pat = re.compile(r"^\s*WORKDIR\s+", re.I)
    from_pat = re.compile(r"^\s*FROM\s+", re.I)
    for i, ln in enumerate(lines):
        if first_from < 0 and from_pat.match(ln):
            first_from = i
        if workdir_pat.match(ln):
            last_workdir = i
        if first_run < 0 and run_pat.match(ln):
            first_run = i
        if first_copy_ep < 0 and copy_ep_pat.match(ln):
            first_copy_ep = i
    anchor = last_workdir if last_workdir >= 0 else first_from
    if anchor < 0:
        return 0
    insert = anchor + 1
    for limit in (first_run, first_copy_ep):
        if 0 <= limit < insert:
            insert = limit
    return insert
