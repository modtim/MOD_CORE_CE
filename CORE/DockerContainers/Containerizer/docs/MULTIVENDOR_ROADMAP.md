# Multi-vendor backend support — roadmap

## Goal and ceiling

Build credible auto-port from a hero (typically CUDA) PyTorch source to
**Intel XPU (Arc/Battlemage/PVC)** and **Intel NPU (OpenVINO via
`optimum-intel`)**, with a coverage target of **80–90 %** of HF-style
inference workloads on those backends. Higher coverage is bounded by
constraints documented in §"Walls" — they are not engineering
problems we can outspend.

CUDA, ROCm and CPU are already at ≈100 % via the simple-recipe path:
ROCm is API-equivalent to CUDA; CPU is a string swap. That work
continues to live in `tools/gpu_variants.py`.

## Walls (why 100 % isn't a target)

| wall | example |
|---|---|
| Custom CUDA kernels | `.cu` / `CUDAExtension`, FlashAttention-2, custom RMSNorm. No tool produces equivalent xpu/npu source. |
| NPU op coverage gaps | Dynamic shapes, sparse ops, exotic activations silently fall back to CPU or fail at convert. |
| Quantization is mandatory + accuracy-sensitive | NPUs need INT8/INT4. No automatic guarantee of post-quant accuracy without per-model calibration data. |
| API divergence | `OVModelForVision2Seq` ≠ `BlipForConditionalGeneration` with a renamed import — different generate semantics, batching, KV cache. |
| Hardware-feature absence | NCCL on ANE, CUDA Graphs on NPU, FP8 outside H100/MI300/PVC. |
| Vendor SDK lifecycle | Apple Core ML is macOS-only; Vitis AI is licensed; QNN pinned to specific kernels. |
| N × M verification cost | 5 backends × thousands of HF architectures = permanent staffing line. |
| Memory/compute fit | 70B LLM doesn't fit on Arc A770; nothing software-side fixes that. |

## Architecture (six layers)

```
                ┌────────────────────────────────────────────┐
                │  source files (user_script.py + entrypoint)│
                └───────────────────┬────────────────────────┘
                                    │
                                    ▼
              1. NEGATIVE CLASSIFIER  (tools/backends/negative_classifier.py)
                       — scans for cuda-namespace calls,
                         custom kernels, flash_attn, triton,
                         NCCL, CUDAExtension, dynamic-shape patterns.
                                    │
                                    ▼
              2. CAPABILITY MATRIX   (tools/backends/capability_matrix.py)
                       — per-backend × per-feature support.
                         Source of truth for "can this run here".
                                    │
                                    ▼
              3. ARCHITECTURE ALLOWLIST  (tools/backends/architectures.py)
                       — HF model classes known to convert per backend.
                                    │
                                    ▼
              4. RECIPE LIBRARY    (tools/backends/recipes/{intel_xpu,intel_npu,…}.py)
                       — pattern-matched rewrite + Dockerfile template.
                                    │
                                    ▼
              5. SMOKE + ACCURACY PROBE  (existing P9e pipeline, extended per backend)
                       — actually load + run on real hardware,
                         diff output vs reference.
                                    │
                                    ▼
              6. MANUAL ESCAPE HATCH  (wizard UX — Phase 4)
                       — when auto-port can't, give the operator the
                         stub + structured edit form + re-verify.
```

## Phases (what ships when)

### Phase 1 — buildable now (no hardware required)

Layers **1, 2, 3, partial 4** ship in this phase. Nothing here needs
Intel/NPU silicon to ship; everything is static analysis + template
rendering + capability declaration.

- `tools/backends/__init__.py`
- `tools/backends/capability_matrix.py` — initial table populated
  from public IPEX 2.x and OpenVINO 2025.x documentation.
- `tools/backends/architectures.py` — `optimum-intel` exported model
  list, IPEX-supported architectures.
- `tools/backends/negative_classifier.py` — pattern scanner. Returns
  `{backend: incompatible_reason}` per source.
- `tools/backends/recipes/intel_xpu.py` — STUB Dockerfile template.
  No source rewrite yet (that's Phase 2).
- `tools/backends/recipes/intel_npu.py` — STUB Dockerfile template
  with the `optimum-cli export` step pre-wired but commented out
  pending hardware verification.
- `tools/create_container.py` — calls into the new package for
  every enabled-but-not-detected variant; emits stub Dockerfile +
  `variant_adaptations[v]` flagged `incompatible: true` with the
  classifier's reason. Wizard's existing incompatible-state UI
  path picks this up automatically (no frontend change needed).

**Phase-1 outcome:** the wizard stops showing "pending forever".
Every variant tab is one of:
- `ready` (real Dockerfile + adapter output, currently CUDA/ROCm/CPU
  on plain-torch sources),
- `incompatible — <specific reason>` (with stub the operator can
  edit if they want to hand-port),
- `not enabled` (variant not in the platform whitelist).

### Phase 2 — Intel XPU recipe (~2–3 weeks; needs Arc A770 / Battlemage)

Layers **4 (deepened) + 5** for Intel XPU.

- `tools/backends/recipes/intel_xpu.py` — full recipe: `import
  intel_extension_for_pytorch as ipex`, `model = ipex.optimize(model)`,
  device-string xpu rewrite. Refuses sources flagged by the negative
  classifier.
- Smoke probe extended for XPU: actually load on Intel hardware,
  run inference, diff against the CUDA reference.
- Architecture allowlist tightened from real CI sweep.

### Phase 3 — Intel NPU recipe (~3–4 weeks; needs Intel Core Ultra)

Layers **4 (deepened) + 5** for Intel NPU + new wizard step for
calibration.

- `tools/backends/recipes/intel_npu.py` — `optimum-cli export
  openvino` at build time, `OVModel*` inference replacement.
- Wizard step: upload representative calibration inputs for INT8.
  Without this, FP32 NPU inference is slower than CPU.
- Architecture allowlist enforced strictly — refuse classes
  `optimum-intel` doesn't export.

### Phase 4 — Manual escape hatch UX (~1 week)

Layer **6** — wizard affordance. When a variant is `incompatible`,
expose the stub Dockerfile + a structured form ("paste xpu equivalent
of these blocker lines") + re-verify button.

### Phase 5+ — other vendors (deferred)

Each is a 3–6 week project with its own hardware:

| vendor | runtime | hw | priority |
|---|---|---|---|
| Qualcomm Hexagon | QNN/SNPE | Snapdragon X dev kit | low |
| AMD Ryzen AI | ONNX Runtime + Vitis AI | Ryzen AI box | low |
| Apple ANE | Core ML | macOS — wrong OS for us | not on roadmap |
| Hailo / Coral / Jetson Orin | vendor SDKs | each device | very low |

## Hardware acquisition list (for Phases 2+)

| phase | hardware | approx cost |
|---|---|---|
| 2 | Intel Arc A770 16 GB | $250 |
| 2 (training) | Intel Battlemage Pro / Arc Pro | $600–900 |
| 3 | Intel Core Ultra 7 (NPU 3) workstation | $1,500–2,500 |
| 5+ | Snapdragon X dev kit, Ryzen AI 9 box, etc. | $1k–3k each |

Without hardware, phases 2+ ship as **"experimental — manual
verification required"** rather than as production features.

## Maintenance posture (after launch)

- **CI hardware fleet** runs the smoke + accuracy probe nightly
  against a curated subset of HF architectures per backend.
- **Capability matrix** auto-refreshed from CI run results.
- **Architecture allowlist** updated weekly from the
  `optimum-intel` and IPEX release notes.
- **Recipe library** receives drive-by fixes as model authors push
  exotic ops into mainline transformers.

Estimated maintenance: ~1 FTE ongoing once Phases 1–3 are shipped.

## Phase 6 audit — kernel_porter telemetry + HIPIFY warning gate (2026-04-24)

**Status:** shipped. This phase was an audit + tighten pass on the already-
wired `kernel_porter.plan_port` → hipify/dpct subprocess path, not an
activation of new code paths. The Containerizer's `_one_port` fan-out at
`create_container.py:433` was already reaching `plan_port` for every
non-CUDA variant; this phase closed gaps in the signal we were collecting
and the safety of "returncode 0 == clean translation" assumptions.

### What changed

1. **HIPIFY warning scan (`kernel_porter.py:1219`).** `_hipify_subprocess`
   previously merged stdout+stderr and ignored warnings entirely — a
   returncode-zero run with 30 `warning: unrecognized CUDA token` lines
   was treated the same as a clean translation. Now splits stdout/stderr
   (new `_run_subprocess_split`), counts `^(?:hipify\s*:\s*)?warning\s*:`
   lines, and returns the total to the caller.

2. **Compatibility downgrade rule.** `_plan_port_hip` consumes the
   warning count; above `MOD_HIPIFY_WARNING_THRESHOLD` (default 3) it
   downgrades `compatibility` from `compatible` → `not_recommended` and
   appends the count to `incompat_reason`. The underlying transforms
   still apply — operator can opt into the imperfect translation via
   the manual-escape-hatch path — but the wizard now surfaces the
   partial-translation signal honestly.

3. **Binary-version baking (`_effective_porter_version`).**
   `plan.port_report.translator_version` now composes as
   `v1:hipify=<ver>` / `v1:dpct=<ver>` so the API's `PortOutcome` writer
   (`worker_tasks.py:2961`) invalidates cached verdicts automatically
   after a toolchain bump. Version is resolved once per Containerizer
   run and cached in `_BINARY_VERSION_CACHE`.

4. **Structured telemetry emitter
   (`scripts/tools/kernel_porter_telemetry.py`).** One
   `[port_telemetry] {json}` stderr line per variant per port pass,
   hooked from `_one_port` via `_emit_port_telemetry_safe`. The line
   carries: tool, compatibility, wall_ms, unsupported_count,
   files_translated, reason, porter_version, hipify_warning_count.
   Flows through existing worker `log_tail` capture → API parser on
   the admin dashboard (Phase 10 consumes this stream).

### Test harness

`CORE/API/tests/test_kernel_porter_pipeline_wired.py` — nine tests
covering:
- `plan_port` reaches both the rocm and npu paths and sets
  `translator_version`,
- HIPIFY warning regex semantics (bare, `HIPIFY:`-prefixed,
  case-insensitive, no false positive on "a warning for you"),
- Env-var threshold override,
- Partial-translation downgrade (5 warnings over threshold 2 →
  `not_recommended` with "5" in reason),
- Under-threshold stays `compatible`,
- Telemetry line round-trips through `parse_telemetry_lines`,
- End-to-end plan → emit → parse.

### Known scope boundaries

- dpct does not return a warning count from `_dpct_subprocess`; it
  parses DPCT warning codes via `_parse_dpct_warnings` and pushes them
  onto `port_report.unsupported_apis`, which already drives the
  not_recommended verdict. Symmetry with hipify is deliberate — SYCL
  warnings are typed (DPCT1007, DPCT1031, …) so they're already
  actionable; HIPIFY warnings are free-text so they needed a numeric
  proxy.

- Telemetry is stderr-only (fire-and-forget). No retries, no buffering.
  A truncated `log_tail` loses events — acceptable because the
  PortOutcome row on the DB is the durable record; telemetry is for
  dashboards, not billing.

- The `MOD_HIPIFY_WARNING_THRESHOLD` default of 3 is a starting guess;
  tune after Phase 10's admin dashboard surfaces real-world warning
  distributions.

