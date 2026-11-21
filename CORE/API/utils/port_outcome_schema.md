# PortOutcome.diagnostics schema

JSONB column on `port_outcomes`. Phase-accrued set of structured
per-port signals the wizard, admin dashboards, and nightly harvesters
consume. Kept intentionally flat (no nesting beyond one level) so
Postgres GIN indexes on individual keys are cheap.

## Fields

| Key | Source phase | Type | Description |
|---|---|---|---|
| `tool` | P9a | string \| null | Human label for the translator that ran (e.g. `"hipify-clang"`, `"dpct"`, `"openvino (torch→ONNX→IR)"`, `"preflight-only"`, `"noop"`). |
| `unsupported_apis` | P9a | list[string] | CUDA APIs the porter flagged as untranslatable. Drives the wizard's "manual fix" prompt + the admin top-failing-APIs dashboard. |
| `manual_cleanup_needed` | P9a | list[string] | Free-text hints the recipe emitted for the LLM cleanup pass to pick up. |
| `arch_mapping` | **P8** | object \| absent | LLM-discovered HF→OpenVINO mapping, only present when the Containerizer's `arch_resolver` supplied a class outside the static allowlist. Shape: `{hf_class, ov_class, task, confidence: 0..1, source: "llm"}`. Consumed by `utils/arch_mapping_index.build_index` + the Phase 10 promotion job. |
| `int8_calibration` | P9 (Phase 9, UX) | object \| absent | When the wizard's INT8 calibration step ran, stashes `{dataset_hash, sample_count, calibration_metrics}`. Lets the admin tooling show per-tenant accuracy vs calibration-set size curves. |
| `translator_disagreement` | P9j | object \| absent | Set when dpct and SYCLomatic shadow-run disagreed on the same input. Shape: `{translators: [name,...], diff_summary, outputs: {name → warning_list}}`. |

## Top-level PortOutcome columns (for cross-reference)

| Column | Description |
|---|---|
| `source_hash` | Tenant-agnostic SHA-256 over the canonical filename:body manifest. |
| `variant` | `"cuda"` \| `"rocm"` \| `"intel"` \| `"npu"` \| `"metal"` \| `"cpu"`. |
| `porter_version` | kernel_porter's effective version including tool fingerprints (P6). Dedup key. |
| `compatibility` | `"compatible"` \| `"not_recommended"` \| `"incompatible"`. |
| `reason` | Incompat/not_recommended reason string, rendered in the wizard's variant tile. |
| `ref_cuda_ratio` | **P11** — measured variant/CPU p50 latency ratio from smoke_probe's perf capture. Null when the perf pass was skipped. |
| `tenant_id` | Nullable FK to tenants; null for system-triggered ports. |
| `created_at` | Row insert time; cutoff for Phase 10 staleness filters. |

## Writers

- `CORE/API/routers/worker_tasks.py::_record_port_outcomes` — persists
  one row per variant from a completed Containerizer task's
  `port_plans` + `variant_adaptations`.
- `CORE/API/routers/worker_tasks.py::_apply_variant_smoke_result` —
  backfills `ref_cuda_ratio` on the most-recent matching row after a
  smoke task completes (P11).

## Readers

- `CORE/API/routers/port_coverage.py` — admin dashboard aggregations.
- `CORE/API/utils/arch_mapping_index.build_index` — Phase 8 HF→OV
  index consumed by the Phase 10 nightly promotion DAG.
- Containerizer detector (via the `/port_outcomes/lookup` service
  endpoint) — preflight-verdict cache for repeat ports.

## Growth controls

A 90-day archival job is scheduled to move old rows off the live
table into cold storage (see Phase 10 plan). Until then, the GIN
indexes + the covering index on `(source_hash, variant,
porter_version)` keep the hot path fast up to ~10M rows.
