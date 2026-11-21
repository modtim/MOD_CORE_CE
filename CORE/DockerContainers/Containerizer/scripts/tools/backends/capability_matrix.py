"""Backend × feature support matrix.

The single source of truth that the negative classifier consults. A
"feature" here is anything the static analyzer can detect in source —
not raw torch ops; the analyzer doesn't run CUDA op coverage probes.
Each cell is one of:

    "full"        — backend supports the feature 1:1 with CUDA semantics
                    (no rewrite or trivial rewrite).
    "rewrite"     — backend supports it but needs a recipe-driven rewrite
                    (e.g. cuda-namespace → xpu-namespace API translation).
                    Phase 2+ recipes turn these from blockers into auto-
                    ports; Phase 1 surfaces them as incompatible with a
                    "rewrite required" reason.
    "blocked"     — backend genuinely can't run the feature even with a
                    rewrite (custom CUDA kernels on NPU, FP8 outside
                    H100/MI300/PVC, etc.). Operator must hand-port or
                    skip.
    "n/a"         — the feature category doesn't apply to this backend
                    (e.g. NCCL on a single-die NPU).

Phase 1 ships with declarations sourced from public IPEX 2.x and
OpenVINO 2025.x docs + the `optimum-intel` README. Phase 2 onwards
auto-refreshes cells from CI sweep results once hardware lands.
"""

from __future__ import annotations

from typing import Dict


# Canonical backend names — must match the variant keys the rest of the
# Containerizer pipeline uses (gpu_variants.ALL_KNOWN_VARIANTS,
# settings.PlatformGPUSettings.hardware_tags).
BACKENDS = ("cuda", "rocm", "cpu", "intel", "npu")

# Feature taxonomy — the static analyzer emits one of these tags per
# pattern it detects. Keep this list in sync with negative_classifier's
# regex/AST checks.
FEATURES = (
    "plain_torch_inference",       # `model.to('cuda')` + simple HF inference
    "plain_torch_training",        # adds backward + optim + dataloader
    "cuda_namespace_explicit",     # `torch.cuda.synchronize()`, `.amp.autocast()` etc.
    "custom_cuda_kernels",         # `.cu` files / `CUDAExtension` in setup.py
    "flash_attn",                  # `from flash_attn import ...`
    "triton_kernels",              # `import triton` (cuda backend only on most builds)
    "nccl_distributed",            # `torch.distributed` w/ nccl backend
    "pin_memory_dataloader",       # `pin_memory=True` (cuda-specific perf)
    "mixed_precision_explicit",    # `torch.cuda.amp.autocast()` w/ explicit dtypes
    "fp8_dtype",                   # `torch.float8_e4m3fn` etc.
    "torch_compile_cuda",          # `torch.compile(mode='reduce-overhead')` on cuda
    "transformers_inference",      # HF transformers AutoModel inference
    "transformers_training",       # HF Trainer / Accelerate
    "diffusers_pipeline",          # HF diffusers pipelines
    "vllm_server",                 # vLLM server-side inference
    "deepspeed",                   # DeepSpeed integration
    "fsdp",                        # PyTorch FSDP
    "quantization_int8",           # bitsandbytes / GPTQ / AWQ
    "openvino_runtime",            # `import openvino` / `optimum.intel`
    "ipex_runtime",                # `import intel_extension_for_pytorch`
    "dynamic_shape_inference",     # variable-length sequences (LLM autoregressive decode)
)


# Capability cells. Read as: SUPPORT[backend][feature] = level.
#
# Sources used for the initial fill (Phase 1):
#   - IPEX 2.6 op coverage doc (Intel)
#   - OpenVINO 2025.0 supported ops + Optimum Intel exporter list
#   - PyTorch ROCm wheel docs (HIP API parity)
#
# Cells with comments mean "non-obvious — see comment for rationale".
SUPPORT: Dict[str, Dict[str, str]] = {
    "cuda": {
        # CUDA is the reference backend; everything is "full" by definition.
        f: "full" for f in FEATURES
    },
    "rocm": {
        # ROCm wheels keep the torch.cuda API surface, so most cuda-shaped
        # source runs unchanged. The exceptions are vendor-specific kernels
        # and CUDA-version-pinned APIs.
        "plain_torch_inference":   "full",
        "plain_torch_training":    "full",
        "cuda_namespace_explicit": "full",   # API parity by design
        "custom_cuda_kernels":     "rewrite",  # hipify works for most
        "flash_attn":              "rewrite",  # flash-attn-rocm fork exists; per-version
        "triton_kernels":          "rewrite",  # triton has rocm backend, not all kernels port
        "nccl_distributed":        "rewrite",  # → RCCL (drop-in via env var)
        "pin_memory_dataloader":   "full",
        "mixed_precision_explicit":"full",
        "fp8_dtype":               "blocked", # MI300 only; not auto-portable
        "torch_compile_cuda":      "rewrite",
        "transformers_inference":  "full",
        "transformers_training":   "full",
        "diffusers_pipeline":      "full",
        "vllm_server":             "rewrite", # vllm-rocm fork
        "deepspeed":               "rewrite", # DeepSpeed has rocm support
        "fsdp":                    "full",
        "quantization_int8":       "rewrite", # bitsandbytes on rocm is patchy
        "openvino_runtime":        "n/a",
        "ipex_runtime":            "n/a",
        "dynamic_shape_inference": "full",
    },
    "cpu": {
        # CPU runs everything that doesn't strictly require an accelerator.
        # The "blocked" cells are features that are gpu-only by design.
        "plain_torch_inference":   "rewrite",  # cuda → cpu device string
        "plain_torch_training":    "rewrite",  # slow but possible for tiny models
        "cuda_namespace_explicit": "blocked",  # torch.cuda.* has no cpu equivalent
        "custom_cuda_kernels":     "blocked",
        "flash_attn":              "blocked",  # implementation is gpu-only
        "triton_kernels":          "blocked",
        "nccl_distributed":        "rewrite",  # → gloo backend
        "pin_memory_dataloader":   "n/a",
        "mixed_precision_explicit":"rewrite",  # cpu autocast exists but limited
        "fp8_dtype":               "blocked",
        "torch_compile_cuda":      "rewrite",
        "transformers_inference":  "full",
        "transformers_training":   "full",    # for tiny models
        "diffusers_pipeline":      "rewrite", # very slow but works
        "vllm_server":             "blocked", # cpu vllm exists but isn't viable for prod
        "deepspeed":               "blocked",
        "fsdp":                    "blocked",
        "quantization_int8":       "rewrite", # CPU has decent int8 support
        "openvino_runtime":        "n/a",
        "ipex_runtime":            "n/a",
        "dynamic_shape_inference": "full",
    },
    "intel": {
        # Intel XPU via IPEX. Most features need a recipe-driven rewrite
        # (.to('xpu'), ipex.optimize(), import intel_extension_for_pytorch).
        # Phase 1 surfaces "rewrite" cells as incompatible with the reason
        # "Intel XPU recipe not yet shipped (Phase 2)". Phase 2 ships
        # the recipe and these flip to "full" on the auto-port path.
        "plain_torch_inference":   "rewrite",
        "plain_torch_training":    "rewrite",
        "cuda_namespace_explicit": "rewrite",  # torch.cuda.* → torch.xpu.*
        "custom_cuda_kernels":     "blocked",  # no auto-translator to SYCL/DPC++
        "flash_attn":              "blocked",  # flash_attn_xpu exists but per-kernel; not auto
        "triton_kernels":          "blocked",  # triton-xpu in beta
        "nccl_distributed":        "rewrite",  # → oneCCL
        "pin_memory_dataloader":   "rewrite",  # different param name
        "mixed_precision_explicit":"rewrite",  # torch.xpu.amp.autocast()
        "fp8_dtype":               "blocked",  # only on PVC
        "torch_compile_cuda":      "rewrite",
        "transformers_inference":  "rewrite",
        "transformers_training":   "rewrite",
        "diffusers_pipeline":      "rewrite",
        "vllm_server":             "blocked",  # vllm-xpu is a separate fork
        "deepspeed":               "rewrite",  # DeepSpeed-xpu plugin exists
        "fsdp":                    "rewrite",
        "quantization_int8":       "rewrite",  # via intel-extension-for-transformers
        "openvino_runtime":        "n/a",
        "ipex_runtime":            "full",
        "dynamic_shape_inference": "full",
    },
    "npu": {
        # Intel NPU via OpenVINO + optimum-intel. The op-set is fixed in
        # silicon, so anything not in optimum's exporter is a hard
        # "blocked". The "rewrite" cells need an offline conversion step
        # at build time and a different inference API (OVModel*).
        "plain_torch_inference":   "rewrite",  # via optimum-cli export openvino
        "plain_torch_training":    "blocked",  # NPUs are inference accelerators
        "cuda_namespace_explicit": "blocked",  # OpenVINO doesn't share API
        "custom_cuda_kernels":     "blocked",
        "flash_attn":              "blocked",
        "triton_kernels":          "blocked",
        "nccl_distributed":        "n/a",     # NPUs are single-die
        "pin_memory_dataloader":   "n/a",
        "mixed_precision_explicit":"blocked", # quant is mandatory, not optional
        "fp8_dtype":               "blocked",
        "torch_compile_cuda":      "blocked",
        "transformers_inference":  "rewrite",  # gated by optimum allowlist
        "transformers_training":   "blocked",
        "diffusers_pipeline":      "rewrite",  # gated; only some pipelines export
        "vllm_server":             "blocked",
        "deepspeed":               "blocked",
        "fsdp":                    "blocked",
        "quantization_int8":       "rewrite",  # required for NPU perf; needs calibration
        "openvino_runtime":        "full",
        "ipex_runtime":            "n/a",
        "dynamic_shape_inference": "blocked",  # most NPUs need static shapes
    },
}


def supports(backend: str, feature: str) -> str:
    """Return the support level for (backend, feature). Defaults to
    'blocked' when the cell is undeclared so callers fail safe."""
    return SUPPORT.get(backend, {}).get(feature, "blocked")


def is_blocked(backend: str, feature: str) -> bool:
    return supports(backend, feature) == "blocked"


def needs_rewrite(backend: str, feature: str) -> bool:
    return supports(backend, feature) == "rewrite"
