// Shared stage inference + formatting for build-progress UI.
//
// Used by the Node Queue panel's per-row component. Was previously
// inline in build-progress-card.tsx; pulled out so multiple UI
// surfaces can share the stage rules without a circular component
// import.

export type Stage =
  | "queued"
  | "generating"
  | "porting"
  | "building"
  | "auto_repairing"
  | "pushing"
  | "registering"
  | "scanning"
  | "done"
  | "failed";

// Keep stage order stable — the progress bar uses the index.
export const STAGE_ORDER: Stage[] = [
  "queued",
  "generating",
  "porting",
  "building",
  "auto_repairing",
  "pushing",
  "registering",
  "scanning",
  "done",
];

export const STAGE_LABEL: Record<Stage, string> = {
  queued:         "Queued",
  generating:     "Generating",
  porting:        "Porting CUDA kernels",
  building:       "Building",
  // The auto-repair loop owns both the inner (script-fix) and outer
  // (Dockerfile-fix) iterations; both surface as "Auto-repairing" so
  // the user sees the wizard doing real work rather than a static
  // "Building" chip during the rebuild pause.
  auto_repairing: "Auto-repairing",
  pushing:        "Pushing",
  registering:    "Registering",
  scanning:       "Scanning",
  done:           "Done",
  failed:         "Failed",
};

// Substring → stage mapping. Ordered: more specific first.
// Parsed from log_tail which accumulates every worker stdout line.
//
// The δ DAG build-task emits log lines with prefixes like
// `[register_node]` / `[push_variant]` / `[build_variant]`. The
// register stage's success line is
// `[register_node] registered NodeDefinition id=<n>` — the
// `registered NodeDefinition` substring is the "advance to scanning"
// signal (matches the bare `[register_node]` for the registering
// chip, then this more-specific match takes over once registration
// has actually completed). Without the more-specific needle the
// chip used to stick at "registering" forever and users thought the
// scan never finished.
const LOG_HINTS: Array<{ needle: string; stage: Stage }> = [
  { needle: "registered NodeDefinition",              stage: "scanning" },
  { needle: "Node registered",                        stage: "scanning" },  // legacy log shape
  { needle: "Step 3/3: Registering",                  stage: "registering" },
  { needle: "Step 2/3: Pushing",                      stage: "pushing" },
  { needle: "Step 1/3: Building",                     stage: "building" },
  { needle: "docker push",                            stage: "pushing" },
  { needle: "docker build",                           stage: "building" },
  // δ DAG build-task uses its own log prefixes. Add them so the
  // workflow-run-aggregated log_tail (from /build-run-progress)
  // advances the stage chip correctly. Order matters: registering
  // runs after pushing which runs after building.
  { needle: "[register_node]",                        stage: "registering" },
  { needle: "[push_variant]",                         stage: "pushing" },
  // Auto-repair loop. The test-and-repair stage emits any of these
  // log lines as it iterates; we want the chip to show "Auto-repairing"
  // whenever the loop is mid-flight (not stuck at "Building"). Order
  // matters: these go above `[build_variant]` so the chip advances off
  // "Building" once the loop starts emitting its own progress.
  { needle: "[test-and-repair] PASS",                 stage: "pushing" },
  { needle: "[test-and-repair] FAIL",                 stage: "failed" },
  { needle: "[test-and-repair] calling LLM",          stage: "auto_repairing" },
  { needle: "[test-and-repair] outer repair",         stage: "auto_repairing" },
  { needle: "[test-and-repair] smoke",                stage: "auto_repairing" },
  { needle: "[test-and-repair] attempt",              stage: "auto_repairing" },
  { needle: "[test-and-repair]",                      stage: "building" },
  { needle: "[build_variant] building",               stage: "building" },
  // P9f.2 — port pass runs after LLM generation and before docker build.
  // Containerizer prints "[port] pass starting (N variants: ...)" when
  // it enters the per-variant hipify/dpct/openvino loop. Keep this
  // ABOVE the "Generating" hints so the stage advances off "Generating"
  // once porting begins, and BELOW "Step 1/3: Building" so the real
  // build takes over as soon as docker build fires.
  { needle: "[port] pass starting",                   stage: "porting" },
  { needle: "Generating Albedo-pattern artifacts",    stage: "generating" },
  { needle: "Using LLM provider",                     stage: "generating" },
  { needle: "Mode:     wrap_image",                   stage: "generating" },
];

export function stageFromLog(logTail: string): Stage {
  if (!logTail) return "queued";
  for (const { needle, stage } of LOG_HINTS) {
    if (logTail.includes(needle)) return stage;
  }
  return "queued";
}

export function stageIndex(s: Stage): number {
  if (s === "failed") return -1;
  const i = STAGE_ORDER.indexOf(s);
  return i === -1 ? 0 : i;
}

export function stagePercent(s: Stage): number {
  if (s === "done" || s === "failed") return 100;
  const i = stageIndex(s);
  return Math.round(((i + 1) / STAGE_ORDER.length) * 100);
}

// ---------------------------------------------------------------------------
// Two-phase model: creating a node runs a Containerizer task ("generation")
// followed by an image-builder task ("build"). The two pipelines have
// non-overlapping stage vocabularies — Containerizer never pushes/scans,
// the image-builder never re-runs LLM generation — so the build-queue row
// shows a different chip strip per phase.
//
// `STAGE_ORDER` above remains the legacy flat union; renderers SHOULD
// route through `stagesForPhase(entry.phase)` which falls back to the
// legacy order for entries missing a `phase` field (back-compat for
// rows in flight during the deploy that introduces this).
// ---------------------------------------------------------------------------

// Phase taxonomy:
//   "generation" — Step 1 of 2: Containerizer materialises Dockerfile
//                  + entrypoint + variants. Image / script / dockerfile
//                  source flows.
//   "build"      — Step 2 of 2: image-builder compiles, pushes, scans,
//                  registers. Always preceded by a "generation" row.
//   "build_only" — Single-step build that has no Phase 1. Used by the
//                  repo (git) source flow, which clones + builds in one
//                  pipeline so there's no separate generation phase. The
//                  row renders without a "Step N of 2" prefix because
//                  there's only one step.
export type BuildPhase = "generation" | "build" | "build_only";

export const STAGE_ORDER_GENERATE: Stage[] = [
  "queued",
  "generating",
  "done",
];

export const STAGE_ORDER_BUILD: Stage[] = [
  "queued",
  // Wizard-as-workflow-native: phase:"build" rows now come from a
  // workflow_run whose first node is system-wizard-generate (the
  // Containerizer LLM). The row spends its early life on "generating"
  // before the build-task variants start; without this entry the
  // log-hint-driven stage flips would peg the progress bar at 100%
  // (stageIndexInPhase falls back to order.length-1 on unknown stages).
  "generating",
  "porting",
  "building",
  "auto_repairing",
  "pushing",
  "registering",
  "scanning",
  "done",
];

export function stagesForPhase(phase: BuildPhase | undefined): Stage[] {
  if (phase === "generation") return STAGE_ORDER_GENERATE;
  // "build" and "build_only" share the same 7-stage build pipeline —
  // they only differ in the row's phase header (with vs without a
  // "Step N of 2" prefix), not in the stages they traverse.
  if (phase === "build" || phase === "build_only") return STAGE_ORDER_BUILD;
  return STAGE_ORDER;
}

export function stageIndexInPhase(s: Stage, phase: BuildPhase | undefined): number {
  if (s === "failed") return -1;
  const order = stagesForPhase(phase);
  const i = order.indexOf(s);
  // Peg at the last stage rather than reset to 0 — a stage we don't
  // recognise in this phase's order means "we're past the chip strip
  // we know how to render". Returning -1 would crash the % math.
  return i === -1 ? order.length - 1 : i;
}

export function stagePercentInPhase(s: Stage, phase: BuildPhase | undefined): number {
  if (s === "done" || s === "failed") return 100;
  const order = stagesForPhase(phase);
  const i = stageIndexInPhase(s, phase);
  return Math.round(((i + 1) / order.length) * 100);
}

// ---------------------------------------------------------------------------
// Porting-chip visibility. The "porting" stage only applies to nodes that
// actually port kernels to another accelerator (torch/CUDA sources). A pure-
// CPU node (Pillow, pandas, an ffmpeg CLI wrap, …) never ports, so a greyed-out
// "Porting CUDA kernels" chip is misleading. The Containerizer emits this
// marker ONLY when it enters the per-variant port loop, so we drive chip
// visibility off the log rather than guessing from static phase.
// ---------------------------------------------------------------------------
export const PORT_LOG_MARKER = "[port] pass starting";

export function hasPortingStarted(logTail: string | undefined | null): boolean {
  return !!logTail && logTail.includes(PORT_LOG_MARKER);
}

export interface StageVisibilityOpts {
  /** True once a real port pass has been observed in the build log. */
  sawPorting?: boolean;
}

// Stages to render for a phase, hiding "porting" until/unless a real port pass
// is observed. Keeps the chip strip honest for CPU-only builds.
export function visibleStagesForPhase(
  phase: BuildPhase | undefined,
  opts?: StageVisibilityOpts,
): Stage[] {
  const stages = stagesForPhase(phase);
  if (opts?.sawPorting) return stages;
  return stages.filter((s) => s !== "porting");
}

// Index / percent computed against the VISIBLE list so the highlight and
// progress bar stay consistent with the rendered chips.
export function visibleStageIndex(
  s: Stage, phase: BuildPhase | undefined, opts?: StageVisibilityOpts,
): number {
  if (s === "failed") return -1;
  const order = visibleStagesForPhase(phase, opts);
  const i = order.indexOf(s);
  return i === -1 ? order.length - 1 : i;
}

export function visibleStagePercent(
  s: Stage, phase: BuildPhase | undefined, opts?: StageVisibilityOpts,
): number {
  if (s === "done" || s === "failed") return 100;
  const order = visibleStagesForPhase(phase, opts);
  const i = visibleStageIndex(s, phase, opts);
  return Math.round(((i + 1) / order.length) * 100);
}

// Runtime invariant: every BuildQueueEntry the renderer sees in the current
// session SHOULD carry a `phase`. Entries from older deploys (before the
// phase field was introduced) legitimately lack one and fall through to the
// legacy chip strip — but a freshly-dispatched row missing `phase` is a
// dispatch-site bug we want to surface. Dev-time `console.warn` per missing
// entry, deduped by taskId so a re-render storm doesn't flood the console.
const _missingPhaseWarned = new Set<number>();

export function assertHasPhase(entry: { taskId: number; nodeName?: string; phase?: BuildPhase }): void {
  if (entry.phase !== undefined) return;
  if (_missingPhaseWarned.has(entry.taskId)) return;
  _missingPhaseWarned.add(entry.taskId);
  console.warn(
    `BuildQueueEntry taskId=${entry.taskId} (${entry.nodeName ?? "unnamed"}) ` +
    `is missing the phase field — dispatch site forgot to stamp it. ` +
    `Renderer falls back to the legacy 8-stage chip strip.`,
  );
}

// Header text per phase:
//   "generation"  → "Step 1 of 2 · Generate files"  (image/script/dockerfile)
//   "build"       → "Step 2 of 2 · Build & register" (image/script/dockerfile)
//   "build_only"  → "Build & register"               (repo flow — no Phase 1)
// Legacy rows without a phase field render with no header (back-compat
// with rows persisted before the phase taxonomy existed).
export function phaseHeaderText(phase: BuildPhase | undefined): string | null {
  if (phase === "generation") return "Step 1 of 2 · Generate files";
  if (phase === "build") return "Step 2 of 2 · Build & register";
  if (phase === "build_only") return "Build & register";
  return null;
}

// ---------------------------------------------------------------------------
// Phase 1 → Phase 2 transition: when the user clicks "Build & register" in
// the wizard, the live row in the queue should morph from
// "Step 1 of 2 · Generate files" into "Step 2 of 2 · Build & register"
// rather than spawning a second row. People expect ONE row per logical
// node-creation task; two rows for one task confuses the queue.
//
// The helper is a pure function over BuildQueueEntry-shaped records so it
// can be unit-tested without React. The renderer's React key is the
// entry's taskId — swapping it on transition deliberately remounts the
// row so the polling effect re-keys onto the new (build) task. nodeName
// is preserved across the transition.
// ---------------------------------------------------------------------------
export interface TransitionableEntry {
  taskId: number;
  nodeName: string;
  startedAt: number;
  outcome?: "success" | "failed";
  phase?: BuildPhase;
  /** Preserved across a Phase 1 → Phase 2 morph so notification
   *  routing can find this row when the user clicks a stale
   *  "artifacts ready" alert keyed on the original generation task id.
   *  See BuildQueueEntry.generationTaskId for why. */
  generationTaskId?: number;
}

export function applyTransitionToBuild<T extends TransitionableEntry>(
  entries: T[],
  generationTaskId: number,
  newBuildTaskId: number,
  nodeName: string,
  startedAt: number = Date.now(),
): T[] {
  // Idempotency: a row already at the build taskId means we've handled
  // this transition. Don't double-mutate.
  if (entries.some((e) => e.taskId === newBuildTaskId)) return entries;

  const idx = entries.findIndex((e) => e.taskId === generationTaskId);
  if (idx === -1) {
    // No Phase 1 row in queue (synchronous flow, or it was dismissed):
    // append a fresh Phase 2 row. Same shape addBuild would produce,
    // PLUS the generationTaskId tag so a stale Phase-1 notification
    // can still locate this row.
    const fresh = {
      taskId: newBuildTaskId,
      nodeName,
      startedAt,
      phase: "build" as BuildPhase,
      generationTaskId,
    } as T;
    return [fresh, ...entries];
  }
  // Mutate the existing Phase 1 row in place: swap taskId, flip phase,
  // clear outcome (was "success" from generation), reset elapsed timer
  // to the build's start so the row's "12s" counter starts at 0.
  // generationTaskId is preserved as the row's `generationTaskId` so
  // the notification bell and the wizard's resume nodeName lookup can
  // both find this row from the original Phase 1 task id.
  const next = entries.slice();
  const prev = next[idx];
  next[idx] = {
    ...prev,
    taskId: newBuildTaskId,
    nodeName,           // explicit re-assert in case caller has a fresher name
    phase: "build",
    outcome: undefined,
    startedAt,
    generationTaskId,
  } as T;
  return next;
}

export function fmtElapsed(ms: number): string {
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m ${s}s`;
}

// Pull a focused slice of failure context from a raw log_tail.
// Used when a row goes to stage=failed — the last FAILED line is
// rarely the root cause, so we show a 3-before-through-25-after
// window around the first error marker.
export function extractFailureContext(raw: string): string[] {
  if (!raw) return [];
  const lines = raw.split("\n").map((s) => s.replace(/\s+$/, ""));
  const markers = /(ERROR|BUILD FAILED|FAILED|failed to solve|Traceback|Unauthorized|Cannot connect|FATAL|401|403)/i;
  let firstErrIdx = -1;
  for (let i = 0; i < lines.length; i++) {
    if (markers.test(lines[i])) {
      firstErrIdx = i;
      break;
    }
  }
  if (firstErrIdx === -1) {
    return lines.filter((l) => l.trim().length > 0).slice(-10);
  }
  const start = Math.max(0, firstErrIdx - 3);
  const end = Math.min(lines.length, firstErrIdx + 25);
  return lines.slice(start, end).filter((l) => l.trim().length > 0);
}
