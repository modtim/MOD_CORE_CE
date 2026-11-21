// Pure helpers that compute which build variants the New Node wizard
// should pre-skip. Two distinct skip sets exist:
//
//   • Review step — `reviewSkippedVariants`. Built from the
//     /predict-variants response (no Phase 1 artifacts yet). Skips
//     anything not in the `suggested` set, anything the worker-side
//     circuit breaker has tripped, and anything the source-level
//     compatibility check rejected.
//
//   • Build step — `skippedVariants`. Seeded once Phase 1 artifacts
//     arrive, from the dockerfile_variants and variant_adaptations
//     blobs. Skips anything the adapter itself flagged
//     `incompatible: true`, plus any variant whose synth-emitted
//     stub Dockerfile carries a `TODO_MODEL_ID` placeholder (which
//     would fail at `optimum-cli export` build time).
//
// The server (build_dag/adapter.py) defends the same way for non-
// UI callers — these helpers keep the UI honest so the user doesn't
// have to manually deselect known-broken variants.

export interface VariantAdaptation {
  incompatible?: boolean;
  files?: Record<string, unknown>;
  [key: string]: unknown;
}

export const TODO_MODEL_ID_MARKER = "TODO_MODEL_ID";

export function computeReviewInitialSkipped(
  variants: ReadonlyArray<string>,
  _suggested: ReadonlyArray<string>,
  circuitBroken: ReadonlyArray<string>,
  incompatibleReasons: Readonly<Record<string, string>>,
  _cpuOnly: boolean = false,
): Set<string> {
  // Pre-skip ONLY for HARD signals from the server:
  //   • circuit-broken on this worker (operational, not predicted), or
  //   • source-level architectural incompatibility (e.g. a CUDA-baked
  //     base image makes ROCm/Intel/NPU genuinely unbuildable).
  //
  // We used to ALSO pre-skip every non-cpu variant when the analyzer
  // LLM classified the script as CPU (`cpuOnly`). That layer was the
  // "fast prediction" path — fragile because it depended on a hard-
  // coded list of imports + binary names + curated GPU libraries to
  // override "CPU" to "CUDA". Scripts that shelled out to colmap /
  // openMVG_main_* / DensifyPointCloud / aliceVision_* (the
  // photogrammetry repro) slipped through and pre-skipped every GPU
  // variant despite being clear GPU workloads.
  //
  // The replacement is EMPIRICAL: the build pipeline (smoke probe with
  // strict gate + GPU-presence verification + variant repair loop)
  // ACTUALLY RUNS each variant under the requested accelerator with
  // `--gpus all` and marks the variant incompatible if the script
  // silently fell back to CPU. So a CUDA variant on a pure-CPU script
  // gets marked incompatible during build — no prediction required.
  //
  // Net effect for users: every variant is pre-checked by default. The
  // build verdict tells the truth, instead of a fragile heuristic
  // guessing. The hard-coded GPU-library lists in
  // node_builder/gpu_library_index.py still UPGRADE process_type for
  // base-image selection, but the UI no longer skips variants on
  // their say-so — they're free upgrades when they fire, not the
  // gating signal.
  //
  // `_suggested` and `_cpuOnly` kept in the signature for backwards-
  // compat callers (and the deprecated CPU pre-skip behavior is gone).
  const circuitBrokenSet = new Set(circuitBroken);
  const skipped = new Set<string>();
  for (const v of variants) {
    if (
      circuitBrokenSet.has(v)
      || (incompatibleReasons && !!incompatibleReasons[v])
    ) {
      skipped.add(v);
    }
  }
  return skipped;
}

export function computeBuildAutoSkipFromArtifacts(
  variantAdaptations: Readonly<Record<string, VariantAdaptation>> | undefined | null,
  dockerfileVariants: Readonly<Record<string, string>> | undefined | null,
): Set<string> {
  const skipped = new Set<string>();
  if (variantAdaptations && typeof variantAdaptations === "object") {
    for (const [variant, adapt] of Object.entries(variantAdaptations)) {
      if (adapt && typeof adapt === "object" && adapt.incompatible) {
        skipped.add(variant);
      }
    }
  }
  if (dockerfileVariants && typeof dockerfileVariants === "object") {
    for (const [variant, dockerfile] of Object.entries(dockerfileVariants)) {
      if (typeof dockerfile === "string" && dockerfile.includes(TODO_MODEL_ID_MARKER)) {
        skipped.add(variant);
      }
    }
  }
  return skipped;
}
