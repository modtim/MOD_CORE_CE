"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, ChevronDown, ChevronRight, FileCode, Loader2, Terminal, X, XCircle } from "lucide-react";
import {
  Stage,
  STAGE_LABEL,
  stageFromLog,
  visibleStagesForPhase,
  visibleStageIndex,
  visibleStagePercent,
  hasPortingStarted,
  phaseHeaderText,
  fmtElapsed,
  extractFailureContext,
  assertHasPhase,
} from "@/lib/build-progress";
import { authenticatedFetch } from "@/lib/api-client";
import { VariantPortDiff } from "./variant-port-diff";
import { VariantFailureModal } from "./variant-failure-modal";
import { CopyLogButton } from "@/components/ui/copy-log-button";
// wizard-rebuild §10 — read-only View Build Plan + Reference Docs
// panels surfaced via a "View build plan" disclosure on the row.
// Imported here so the components compile into the bundle; the
// disclosure is gated on `entry.buildSpecId` being present.
import { BuildPlanPanel } from "./build-plan-panel";
import { RepoDocsPanel } from "./repo-docs-panel";

export interface BuildQueueEntry {
  taskId: number;
  nodeName: string;
  startedAt: number;
  outcome?: "success" | "failed";
  /** "build" rows (undefined for legacy) polls the image-builder task
   *  through queued → building → pushing → registering → done.
   *  "generation" rows represent a wizard Containerizer task that was
   *  deferred via Close & notify me; they poll the same
   *  /generation-artifacts endpoint but the UI surfaces a Resume
   *  button on completion that reopens the wizard at Preview.
   *  "build_only" rows are single-step builds from the repo (git)
   *  flow — same chip strip + polling as "build" but the row header
   *  reads just "Build & register" instead of "Step 2 of 2 · Build &
   *  register" because there's no Phase 1 in this flow. */
  phase?: "generation" | "build" | "build_only";
  /** For build-phase rows that were morphed in place from a Phase 1
   *  generation row (via applyTransitionToBuild), this preserves the
   *  *original* generation task id. Notification routing
   *  (findBuildRowForNotification) and the wizard-resume nodeName
   *  lookup both key off the generation task id present in a stale
   *  "artifacts ready" notification — without this, the lookup
   *  misses the morphed row and the bell reopens the wizard at
   *  Preview, letting the user re-dispatch by mistake. Undefined for
   *  rows that never had a Phase 1 (build_only) or that started life
   *  as a Phase 2 row directly. */
  generationTaskId?: number;
}

interface RowProps {
  entry: BuildQueueEntry;
  accessToken: string | null;
  apiUrl: string;
  onFinished: (taskId: number, outcome: "success" | "failed") => void;
  onDismiss: (taskId: number) => void;
  /** When a generation-phase row completes, the panel surfaces a
   *  Resume button that reopens the wizard at the Preview step with
   *  this task_id. No-op for build-phase rows. */
  onResumeGeneration?: (taskId: number) => void;
  /** When a Phase 2 row terminally failed AND we have structured
   *  failure context, the row surfaces a "Retry with corrections"
   *  button that asks the parent to dispatch a new Containerizer
   *  task with the failure context appended (issue #2). The parent
   *  is responsible for adding the new task as a row + dismissing
   *  the failed one. */
  onRetryWithCorrections?: (failedTaskId: number) => void;
}

/**
 * One build's progress row. Owns its own poll state so a stage change
 * in this row doesn't re-render siblings. The panel's sibling rows
 * skip re-render via the React.memo equality fn below — only a change
 * in this entry's taskId or outcome counts as a reason to re-render.
 *
 * Internal state (stage, lastLog, elapsed, failingLines) is row-local
 * and never flows into the parent's `builds` array, so parent renders
 * don't cascade either.
 */
// P9e.4 — per-variant smoke verification state. Shape matches the
// API response from /generation-artifacts: {variant: status}. A
// variant in `gpu_variants_registered` but missing from this map
// is treated as "pending" (never smoked yet).
type VariantVerificationStatus =
  | "pending"
  | "verified"
  | "failed"
  | "numerical_drift"
  | "build_failed";

// P9o — structured per-variant failure explanation. Produced by the
// API's port_failure_explain helper (LLM / pattern / default) and
// surfaced via /generation-artifacts.failure_explanations.
type FailureExplanation = {
  reason: string;
  manual_fix: string;
  log_excerpt: string;
  source: string;
  original_reason?: string;
};

// Per-variant build result emitted by the ImageBuilder script as a
// `BUILD_RESULT_JSON: {...}` log line (one per variant on completion).
// Issue #2: the row used to surface only free-text BUILD FAILED dumps
// burying the actual root cause. With these structured records the
// row can render a per-variant failure card naming the offending
// command + line + curated error_class.
type BuildResult = {
  variant: string;
  status: "succeeded" | "failed";
  stage: string;
  dockerfile: string;
  line: number | null;
  command: string | null;
  error: string | null;
  error_class:
    | "from_image_not_found"
    | "pip_install_failed"
    | "apt_install_failed"
    | "vcs_clone_failed"
    | "network_unreachable"
    | "out_of_disk"
    | "unknown";
};

/** One-line user-facing explanation per error class. Keep in sync
 *  with the ImageBuilder failure_classifier ERROR_CLASSES tuple. */
const ERROR_CLASS_LABEL: Record<BuildResult["error_class"], string> = {
  from_image_not_found:
    "Base image tag not found in the registry — the Dockerfile's FROM line points at an image that doesn't exist.",
  pip_install_failed:
    "pip install failed — usually a wrong version pin, missing wheel, or transitive conflict.",
  apt_install_failed:
    "apt-get install failed — package not in the base image's repos, or apt cache not refreshed.",
  vcs_clone_failed:
    "git clone failed — wrong URL, missing credentials, or network blip.",
  network_unreachable:
    "Network unreachable from inside the build — DNS or routing problem on the worker.",
  out_of_disk: "Worker ran out of disk while building — free space and retry.",
  unknown: "Build step failed — see the offending command below.",
};

/** Extract BUILD_RESULT_JSON entries from a log tail. Returns the
 *  most recent record per variant (later lines overwrite earlier).
 *  Robust to truncation and malformed JSON — best-effort. */
export function parseBuildResults(logTail: string): Record<string, BuildResult> {
  if (!logTail) return {};
  const out: Record<string, BuildResult> = {};
  const re = /^BUILD_RESULT_JSON:\s*(\{.*?\})\s*$/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(logTail)) !== null) {
    try {
      const parsed = JSON.parse(m[1]) as Partial<BuildResult>;
      if (
        parsed && typeof parsed.variant === "string" &&
        (parsed.status === "succeeded" || parsed.status === "failed")
      ) {
        out[parsed.variant] = parsed as BuildResult;
      }
    } catch {
      /* ignore malformed JSON — next entry overwrites anyway */
    }
  }
  return out;
}

/**
 * Inspect a build task's log tail for the ImageBuilder's final
 * registration outcome. The script exits 0 even when registration is
 * rejected (e.g. admission_rule: image-ref-not-allowlisted), so the
 * task-level status alone reads as "completed". Without this we'd
 * surface a green "done" row for a build that didn't actually
 * register.
 *
 * Returns:
 *   `{registered: true}`  when no failure markers are present.
 *   `{registered: false, reason}`  when a registration-failure or
 *      build-failure line appears in the log.
 */
export function parseBuildResult(logTail: string): { registered: boolean; reason?: string } {
  if (!logTail) return { registered: true };

  // 1. Most precise: the script's final-line JSON. Look for
  //    `"registered": false` and capture the surrounding line for
  //    context. The script writes this as the very last line.
  const jsonMatch = logTail.match(/"registered"\s*:\s*false[^\n]*/i);
  if (jsonMatch) {
    return { registered: false, reason: jsonMatch[0].slice(0, 200) };
  }

  // 2. Pattern markers in the script's stdout. These all indicate the
  //    pipeline finished with at least one fatal step.
  const linePatterns: Array<RegExp> = [
    /Registration failed[^\n]*/i,
    /admission_rule:\s*image-ref-not-allowlisted[^\n]*/i,
    /PARTIAL SUCCESS:[^\n]*\d+\s+failed[^\n]*/i,
  ];
  for (const re of linePatterns) {
    const m = logTail.match(re);
    if (m) return { registered: false, reason: m[0].slice(0, 200) };
  }

  return { registered: true };
}

// wizard-rebuild §10 — ETA + stall response from
// `GET /woodsman/build-specs/{id}/eta`. Polled on mount
// + every 30s while the row is RUNNING.
interface BuildSpecEta {
  spec_id: number;
  eta_seconds: number;
  eta_minutes: number;
  variant_count: number;
  stall_reason: string | null;
}


function BuildQueueRowInner({ entry, accessToken, apiUrl, onFinished, onDismiss, onResumeGeneration, onRetryWithCorrections }: RowProps) {
  // wizard-rebuild §10 — View Build Plan disclosure + ETA + stall.
  // `buildSpecId` is the optional id the parent passes when the
  // queue entry maps to a BuildSpecRecord (every §3+ wizard build).
  // Pre-§3 legacy rows don't have it — disclosure stays hidden.
  const buildSpecId: number | null = (entry as { buildSpecId?: number | null }).buildSpecId ?? null;
  const [showBuildPlan, setShowBuildPlan] = useState(false);
  const [planData, setPlanData] = useState<Record<string, unknown> | null>(null);
  const [docsChunks, setDocsChunks] = useState<unknown[] | null>(null);
  const [etaInfo, setEtaInfo] = useState<BuildSpecEta | null>(null);
  // Recovery action menu open state (§7d / §10d trio).
  const [showRecovery, setShowRecovery] = useState(false);

  // Runtime invariant: every dispatched row should carry a phase. Warns
  // once per session per offending taskId; the renderer keeps working via
  // the legacy chip strip.
  assertHasPhase(entry);
  const [stage, setStage] = useState<Stage>(entry.outcome === "success"
    ? "done"
    : entry.outcome === "failed"
      ? "failed"
      : "queued");
  const [lastLog, setLastLog] = useState("");
  const [elapsed, setElapsed] = useState(Date.now() - entry.startedAt);
  const [failingLines, setFailingLines] = useState<string[]>([]);
  // True once the build log shows a real kernel-port pass. CPU-only nodes
  // never port, so the "Porting CUDA kernels" chip stays hidden for them.
  const [sawPorting, setSawPorting] = useState(false);
  // Sub-progress across the multi-variant build. ImageBuilder prints
  // "Step N/M: Building <variant> (Dockerfile.x)..." for every one of
  // the 12-13 Dockerfile variants. Parsing those from the log lets us
  // show "3 of 13 variants built" instead of "building" for the
  // whole 10-minute run.
  const [variantProgress, setVariantProgress] = useState<{
    current: number;
    total: number;
    variantName: string | null;
  } | null>(null);
  // P9e.4 — smoke verification. Populated from /generation-artifacts
  // on every poll (during AND after the main build). Drives the
  // per-variant badge strip rendered below the progress bar.
  const [variantVerificationStatus, setVariantVerificationStatus] = useState<
    Record<string, VariantVerificationStatus>
  >({});
  const [gpuVariantsRegistered, setGpuVariantsRegistered] = useState<
    Record<string, string>
  >({});
  const [incompatibleReasons, setIncompatibleReasons] = useState<
    Record<string, string>
  >({});
  // P9o — structured failure explanations (LLM / pattern / default) per
  // variant. Drives the failure modal opened by clicking a red badge.
  const [failureExplanations, setFailureExplanations] = useState<
    Record<string, FailureExplanation>
  >({});
  const [failureModalVariant, setFailureModalVariant] = useState<string | null>(null);
  // Issue #2: per-variant structured BUILD_RESULT_JSON records,
  // parsed client-side from log_tail (the ImageBuilder emits them
  // one per variant on completion). Drives the per-variant failure
  // card panel rendered below the variant badges.
  const [buildResults, setBuildResults] = useState<Record<string, BuildResult>>({});
  // Log drawer — expandable section below the progress bar. Shows live
  // logs via /workers/tasks/{id}/container-logs (reads from Redis
  // during running, falls back to stored log_tail on completion).
  const [logsOpen, setLogsOpen] = useState(false);

  // Listen for `mod:queue:expand-row` from the notifications-bell so a
  // notification click for a node whose Phase-2 row already exists can
  // jump straight to that row's logs (instead of reopening the wizard
  // at Preview and asking the user to click Confirm & Build again).
  useEffect(() => {
    if (typeof window === "undefined") return;
    const handler = (evt: Event) => {
      const detail = (evt as CustomEvent<{ taskId?: number }>).detail;
      if (detail?.taskId === entry.taskId) {
        setLogsOpen(true);
      }
    };
    window.addEventListener("mod:queue:expand-row", handler);
    return () => window.removeEventListener("mod:queue:expand-row", handler);
  }, [entry.taskId]);
  // P9n.2 — port diff modal. Only shown when the wizard actually ran
  // the kernel porter (deep-CUDA repos); open-and-see-empty is fine too.
  const [portDiffOpen, setPortDiffOpen] = useState(false);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [logsLoading, setLogsLoading] = useState(false);
  const [logSource, setLogSource] = useState<string>("");
  const logEndRef = useRef<HTMLDivElement>(null);
  const calledFinishedRef = useRef(entry.outcome != null);
  // Flips true after the first /container-logs fetch resolves so the
  // logs panel doesn't flash the spinner on every 2s poll. Without
  // this, an empty log stream rendered "Loading…" / "Waiting for the
  // first log line" alternately every poll.
  const hasInitialFetchedRef = useRef(false);
  // Phase 1 artifacts-ready gate (issue #8). The Containerizer task
  // can flip to status=completed before the LLM has flushed the
  // canonical Dockerfile to the API's response payload. Without
  // gating on `data.dockerfile`, the row goes green + "done" + shows
  // the Resume button, but Resume reopens the wizard with empty
  // artifacts. Hold the row visually at "Finalizing…" / 95% until
  // either the artifact arrives OR we miss it 3 polls in a row
  // (~9s of polling at 3s cadence — transient DB lag falls below
  // this threshold; persistent absence falls through with a caveat).
  const [artifactsPending, setArtifactsPending] = useState(false);
  const [artifactsCaveat, setArtifactsCaveat] = useState(false);
  const artifactsPendingMissCountRef = useRef(0);

  // Poll-resilience counters. The row used to silently drop
  // resp.ok=false responses (auth token expired mid-build, transient
  // 5xx, network blip) and stay in a non-terminal stage indefinitely
  // while the elapsed timer kept ticking. Repro 2026-05-31 spec=12:
  // workflow_run was FAILED for 6+ minutes while the row still showed
  // "Auto-repairing | 16m 25s". `pollFailCount` counts consecutive
  // poll failures; `stuck` flips when either (a) we're past the
  // hard ceiling without reaching a terminal state OR (b) we've
  // missed too many polls in a row. Stuck stops the elapsed timer
  // and surfaces a clearer status to the user.
  const [pollFailCount, setPollFailCount] = useState(0);
  const [stuck, setStuck] = useState(false);

  // Hard wall-clock ceiling for a single build row. Workflow runs
  // that take longer than this are either genuinely stuck, lost their
  // status update path, or are repair-looping pathologically. After
  // this point the row stops claiming "still running" — the elapsed
  // timer freezes, the chip shows "stuck", and the user can refresh.
  // Generous (60 min) so legitimate long builds aren't bothered.
  const STUCK_TIMEOUT_MS = 60 * 60 * 1000;

  // Elapsed-time ticker. Stops when the build is done/failed/stuck.
  useEffect(() => {
    if (stage === "done" || stage === "failed" || stuck) return;
    const id = window.setInterval(() => {
      const next = Date.now() - entry.startedAt;
      setElapsed(next);
      // Trip the stuck flag at the hard ceiling.
      if (next >= STUCK_TIMEOUT_MS) setStuck(true);
    }, 1000);
    return () => window.clearInterval(id);
  }, [entry.startedAt, stage, stuck]);

  // Poll progress every 3s until done/failed. Endpoint depends on
  // phase: generation rows poll /generation-artifacts/{task_id}
  // (ApiTaskQueue-scoped, legacy), build / build_only rows poll
  // /build-run-progress/{workflow_run_id} (WorkflowRun-scoped) — the
  // DAG-based phase-2 dispatch returns a workflow_run_id, not a task
  // id, so the legacy endpoint either 404s or collides with an
  // unrelated old task (previously surfaced as "Queued / 0s" + stale
  // log lines from a 4-day-old task in the user's TRELLIS.2 attempt).
  // Uses the shared authenticatedFetch so an expired access token is
  // silently refreshed mid-poll — without it, the row 401's forever
  // once the token lifetime runs out and the panel shows no data.
  const poll = useCallback(async () => {
    if (!accessToken) return;
    try {
      const path = (entry.phase === "build" || entry.phase === "build_only")
        ? `build-run-progress/${entry.taskId}`
        : `generation-artifacts/${entry.taskId}`;
      const resp = await authenticatedFetch(
        `${apiUrl}/woodsman/${path}`,
        { headers: { Authorization: `Bearer ${accessToken}` } },
      );
      if (!resp.ok) {
        // Track consecutive failures so a non-terminal row that's
        // silently been 401/403/5xx-ing for a while gets demoted to
        // "stuck" instead of pretending to still be running. Threshold
        // is 10 misses (~30s at 3s cadence, ~1.5 min at the 5s post-
        // done cadence) — generous enough to absorb transient
        // backend hiccups, tight enough to surface a real outage
        // before the user wonders why their build is "frozen".
        setPollFailCount((n) => {
          const next = n + 1;
          if (next >= 10) setStuck(true);
          return next;
        });
        if (resp.status === 0 || resp.status >= 500 || resp.status === 401 || resp.status === 403) {
          // Best-effort diagnostic so future stuck-row repros surface
          // in the console rather than vanishing into the void.
          console.warn(
            `[build-queue-row] poll failed for ${entry.taskId}: HTTP ${resp.status} (consecutive misses: ${pollFailCount + 1})`,
          );
        }
        return;
      }
      // Reset the miss counter on any successful response so a
      // recovered backend doesn't keep us pinned in stuck state.
      if (pollFailCount > 0) setPollFailCount(0);
      const data = await resp.json();
      const logTail: string = typeof data.log_tail_tail === "string"
        ? data.log_tail_tail
        : (data.log_tail || "");

      if (logTail) {
        // Reveal the porting chip only once a real port pass is observed;
        // sticky so it stays visible for the rest of a GPU build.
        if (hasPortingStarted(logTail)) setSawPorting(true);
        const lines = logTail.split("\n").map((s: string) => s.trim()).filter(Boolean);
        if (lines.length > 0) setLastLog(lines[lines.length - 1].slice(0, 180));

        // Parse BUILD_RESULT_JSON envelopes the ImageBuilder emits
        // per variant. Each later poll's logTail contains the union
        // of past output, so we just replace state with the latest
        // parse — no diffing required.
        const parsed = parseBuildResults(logTail);
        if (Object.keys(parsed).length > 0) {
          setBuildResults(parsed);
        }

        // Extract multi-variant build progress. ImageBuilder emits
        //   "Step N/M: Building <variant> (Dockerfile.x)..."
        // one line per variant. We scan for the HIGHEST N seen so far.
        const variantRe = /Step (\d+)\/(\d+):\s+Building\s+([a-zA-Z0-9_-]+)/g;
        let m: RegExpExecArray | null;
        let best: { current: number; total: number; variantName: string | null } | null = null;
        while ((m = variantRe.exec(logTail)) !== null) {
          const cur = parseInt(m[1], 10);
          const tot = parseInt(m[2], 10);
          if (!best || cur > best.current) {
            best = { current: cur, total: tot, variantName: m[3] };
          }
        }
        if (best) setVariantProgress(best);
      }

      // P9e.4 — capture per-variant verification state. Attached on
      // every poll by the API (whether the build is still running
      // or already complete) so we can render badges as soon as the
      // smoke pipeline starts flipping statuses.
      if (data && typeof data === "object") {
        if (data.variant_verification_status && typeof data.variant_verification_status === "object") {
          setVariantVerificationStatus(data.variant_verification_status as Record<string, VariantVerificationStatus>);
        }
        if (data.gpu_variants_registered && typeof data.gpu_variants_registered === "object") {
          setGpuVariantsRegistered(data.gpu_variants_registered as Record<string, string>);
        }
        if (data.incompatible_reasons && typeof data.incompatible_reasons === "object") {
          setIncompatibleReasons(data.incompatible_reasons as Record<string, string>);
        }
        if (data.failure_explanations && typeof data.failure_explanations === "object") {
          setFailureExplanations(data.failure_explanations as Record<string, FailureExplanation>);
        }
      }

      // API returns the enum's .value (uppercase: PENDING / CLAIMED /
      // RUNNING / COMPLETED / FAILED / CANCELLED). Normalise so our
      // comparisons don't care about case.
      const statusLower = String(data.status || "").toLowerCase();

      if (statusLower === "failed" || statusLower === "cancelled") {
        setStage("failed");
        setFailingLines(extractFailureContext(logTail));
        // ALWAYS fire onFinished for failed — even if a prior poll
        // wrongly fired onFinished(success) (orchestrator-side race
        // where workflow_run.status was COMPLETED before a child task's
        // FAILED status committed). The parent's outcome state must
        // reflect the latest truth, otherwise localStorage persists
        // "success" for an actually-failed build and the queue lies
        // forever across page reloads.
        if (!calledFinishedRef.current || stage === "done") {
          calledFinishedRef.current = true;
          onFinished(entry.taskId, "failed");
        }
        return;
      }
      if (statusLower === "completed") {
        // Phase 1 artifacts-ready gate (issue #8). Generation tasks
        // can be marked completed before the canonical Dockerfile is
        // flushed to the response payload. Hold the row visually at
        // "Finalizing…" / 95% until `data.dockerfile` arrives. Fall
        // through after 3 consecutive misses with a small caveat
        // note so the Resume button isn't blocked indefinitely on a
        // persistent backend miss.
        if (entry.phase === "generation" && !data.dockerfile) {
          artifactsPendingMissCountRef.current += 1;
          if (artifactsPendingMissCountRef.current < 3) {
            setArtifactsPending(true);
            return; // keep polling next tick
          }
          // 3+ consecutive misses → render terminal done with caveat.
          setArtifactsCaveat(true);
        }
        // Reset gate state on the happy path so a re-render doesn't
        // re-show "Finalizing…" after artifacts arrived.
        setArtifactsPending(false);
        artifactsPendingMissCountRef.current = 0;

        // parseBuildResult scans the IMAGEBUILDER's `registered`
        // marker — only emitted by the legacy /build-and-register-local
        // pipeline that hits the imagebuilder container. The δ DAG
        // generation phase (Containerizer task) doesn't emit it AND
        // the generation-artifacts endpoint doesn't include log_tail
        // in its response when artifacts are found — so logTail here
        // is "", parseBuildResult returns {registered: true} via the
        // empty-input early-return, and we fall through to done.
        // For BUILD phase tasks the legacy script DOES emit it. Only
        // run this check when we're on a build/build_only row.
        // Without this guard, a regression that returns
        // {registered: false} on an empty logTail would silently
        // strand every generation row.
        const isBuildPhase = entry.phase === "build" || entry.phase === "build_only";
        if (isBuildPhase) {
          const result = parseBuildResult(logTail);
          if (!result.registered) {
            setStage("failed");
            const ctx = extractFailureContext(logTail);
            setFailingLines(result.reason ? [result.reason, ...ctx] : ctx);
            if (!calledFinishedRef.current) {
              calledFinishedRef.current = true;
              onFinished(entry.taskId, "failed");
            }
            return;
          }
        }
        setStage("done");
        if (!calledFinishedRef.current) {
          calledFinishedRef.current = true;
          onFinished(entry.taskId, "success");
        }
        return;
      }

      // Still in progress. log_tail is typically empty until completion
      // (the worker only reports logs on terminal state), so we can't
      // infer a precise stage mid-flight. Map the task-queue status to
      // the best-guess stage:
      //   pending        → queued (no worker yet)
      //   claimed        → generating (worker has claimed, spinning up)
      //   running        → building (worker is actively executing)
      // If log_tail DOES contain hints, let those take over for a more
      // accurate sub-stage.
      const hintStage = logTail ? stageFromLog(logTail) : null;
      if (hintStage && hintStage !== "queued") {
        setStage(hintStage);
      } else if (statusLower === "running") {
        setStage("building");
      } else if (statusLower === "claimed") {
        setStage("generating");
      } else {
        setStage("queued");
      }
    } catch {
      // Silent — next tick retries.
    }
  }, [accessToken, apiUrl, entry.taskId, onFinished, pollFailCount]);

  // Once `stage` reaches "done" we used to drop polling entirely.
  // That's the "lying UI" failure mode: if the server's /build-run-
  // progress endpoint briefly reported "completed" while a downstream
  // child task was still in-flight (or got mis-marked during an
  // orchestrator race), the row pinned itself to green Done and never
  // saw the eventual FAILED. The server-side `/build-run-progress`
  // endpoint now demotes any "completed" with FAILED child tasks to
  // "failed", but as belt-and-suspenders the client also keeps polling
  // for ~30 s post-Done so a late-arriving FAILED still flips the chip.
  // `failed` is a true terminal state — once the server says failed,
  // it doesn't un-fail. Done from the server can still flip; failed
  // cannot.
  useEffect(() => {
    if (stage === "failed" || stuck) return;
    poll();
    const intervalMs = stage === "done" ? 5000 : 3000;
    const id = window.setInterval(poll, intervalMs);
    // After 30 s of polling post-Done with no FAILED arrival we stop.
    // 30 s comfortably covers any orchestrator-side reconciliation
    // delay where the run was marked COMPLETED before a child task's
    // FAILED status got committed.
    let stopTimer: number | null = null;
    if (stage === "done") {
      stopTimer = window.setTimeout(() => {
        window.clearInterval(id);
      }, 30000);
    }
    // Wake-on-visibility: Chrome throttles setInterval to ≤1/min in
    // background tabs. Without this, a user who Cmd-tabs away during
    // a build can come back to see the row stuck on stale state for
    // many minutes after the actual task finished. Force an
    // immediate poll the moment the tab regains focus.
    // Stage repro: TRELLIS.2 task 12175 completed in 2.2 min; the
    // user's tab showed "Generating | 30+ min" because polls were
    // minutes apart while backgrounded.
    const onVis = () => {
      if (document.visibilityState === "visible") {
        poll();
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.clearInterval(id);
      if (stopTimer !== null) window.clearTimeout(stopTimer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [poll, stage]);

  // P9e.4 — post-build smoke-verification poll. The main poll above
  // stops at stage=done; but variant_smoke tasks keep running on
  // matching-hardware workers after the ImageBuilder push. Keep
  // polling (slower cadence — 5s) so badges flip from `verifying` to
  // `verified`/`failed` as smoke completes. Stops once every
  // registered variant is in a terminal state.
  useEffect(() => {
    if (stage !== "done") return;
    const registered = Object.keys(gpuVariantsRegistered || {});
    if (registered.length === 0) return;
    const anyPending = registered.some((v) => {
      const s = variantVerificationStatus[v];
      return s === "pending" || s === undefined;
    });
    if (!anyPending) return;
    const id = window.setInterval(poll, 5000);
    return () => window.clearInterval(id);
  }, [stage, gpuVariantsRegistered, variantVerificationStatus, poll]);

  // Live logs fetcher — only runs when the logs panel is open for
  // THIS row. Backs off to a slow poll when the task is done (no new
  // content coming in).
  const fetchLogs = useCallback(async () => {
    if (!accessToken) return;
    if (!hasInitialFetchedRef.current) setLogsLoading(true);
    try {
      // Route by phase: a build/build_only row's entry.taskId is a
      // workflow_run_id (from /build-and-register-node), so the
      // task-keyed endpoint would either 404 OR collide with an
      // unrelated old ApiTaskQueue row whose id happens to match.
      // The workflow-runs variant aggregates Redis + result_payload
      // logs across every child task.
      const path = (entry.phase === "build" || entry.phase === "build_only")
        ? `workers/workflow-runs/${entry.taskId}/container-logs`
        : `workers/tasks/${entry.taskId}/container-logs`;
      const resp = await authenticatedFetch(
        `${apiUrl}/${path}`,
        { headers: { Authorization: `Bearer ${accessToken}` } },
      );
      if (!resp.ok) return;
      const data = await resp.json();
      if (Array.isArray(data.lines)) setLogLines(data.lines);
      if (data.source) setLogSource(data.source);
    } catch {
      /* silent */
    } finally {
      hasInitialFetchedRef.current = true;
      setLogsLoading(false);
    }
  }, [accessToken, apiUrl, entry.taskId]);

  useEffect(() => {
    if (!logsOpen) return;
    fetchLogs();
    // Poll cadence: 2s while the build is live, 10s once it's done
    // (the user may have opened logs on a finished row just to read
    // through).
    const interval = (stage === "done" || stage === "failed") ? 10_000 : 2_000;
    const id = window.setInterval(fetchLogs, interval);
    return () => window.clearInterval(id);
  }, [logsOpen, fetchLogs, stage]);

  // Auto-scroll to bottom when new lines arrive, unless the user has
  // scrolled up to read old content.
  useEffect(() => {
    if (!logsOpen) return;
    logEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [logLines.length, logsOpen]);

  // Hide the "Porting CUDA kernels" chip unless a real port pass ran (CPU-only
  // nodes never port). Index/percent derive from the SAME visible list so the
  // highlight + progress bar stay consistent with the rendered chips.
  const stages = visibleStagesForPhase(entry.phase, { sawPorting });
  const idx = visibleStageIndex(stage, entry.phase, { sawPorting });
  // Honour the Phase 1 artifacts-pending gate: while pending, peg the
  // bar at 95% (don't fill green) and the chip strip rendering picks
  // up a "Finalizing…" overlay below.
  const pct = artifactsPending ? 95 : visibleStagePercent(stage, entry.phase, { sawPorting });
  const phaseHeader = phaseHeaderText(entry.phase);

  // One-shot console.warn if the stage isn't in the phase's order. Pegging
  // (idx === stages.length - 1) hides the issue in the UI, so we surface it
  // for log-classifier debugging without crashing the row.
  const stageInOrder = stages.indexOf(stage) >= 0 || stage === "failed";
  const warnedRef = useRef(false);
  useEffect(() => {
    if (!stageInOrder && !warnedRef.current) {
      warnedRef.current = true;
      console.warn(
        `BuildQueueRow: stage '${stage}' not in '${entry.phase ?? "legacy"}' order; pegging at last stage`,
      );
    }
  }, [stageInOrder, stage, entry.phase]);

  // One-shot console.warn if a row arrives with no nodeName. The
  // fallback "Task #<id>" still renders, so the row is usable, but
  // the user-chosen name was dropped somewhere upstream — typically
  // a Phase 1 → Phase 2 handoff that didn't carry the name through.
  // Surface it so the regression is easy to attribute when grepping
  // browser logs.
  const warnedNoNameRef = useRef(false);
  useEffect(() => {
    if (!entry.nodeName && !warnedNoNameRef.current) {
      warnedNoNameRef.current = true;
      console.warn(
        `BuildQueueRow: entry taskId=${entry.taskId} phase=${entry.phase ?? "legacy"} has no nodeName; falling back to "Task #${entry.taskId}". The user-chosen name was likely dropped between phases.`,
      );
    }
  }, [entry.nodeName, entry.taskId, entry.phase]);

  // wizard-rebuild §10 — fetch ETA + stall status from the BuildSpec
  // endpoint. Polls every 30s while the row is RUNNING; one-shot
  // when the row is already done. Failures degrade silently — the
  // ETA / stall UI is informational, not load-bearing.
  useEffect(() => {
    if (!buildSpecId || !accessToken) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const resp = await authenticatedFetch(
          `${apiUrl}/woodsman/build-specs/${buildSpecId}/eta`,
        );
        if (resp && resp.ok) {
          const j = (await resp.json()) as BuildSpecEta;
          if (!cancelled) setEtaInfo(j);
        }
      } catch {
        // ignore
      }
    };
    tick();
    if (stage === "done" || stage === "failed") return;
    const id = window.setInterval(tick, 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [buildSpecId, accessToken, apiUrl, stage]);

  // wizard-rebuild §10 — fetch BuildPlan + repo-docs chunks when the
  // user opens the disclosure. Lazy — keeps the row light when no
  // one's expanded it.
  useEffect(() => {
    if (!showBuildPlan || !buildSpecId || !accessToken) return;
    if (planData) return;  // already fetched
    let cancelled = false;
    (async () => {
      try {
        const resp = await authenticatedFetch(
          `${apiUrl}/woodsman/build-specs/${buildSpecId}`,
        );
        if (!resp || !resp.ok) return;
        const spec = await resp.json();
        if (cancelled) return;
        setPlanData(spec?.build_plan || null);
        // Repo-docs chunks aren't on the spec — fetch by the
        // `repo_docs_extract_id` if present.
        const docsId = spec?.repo_docs_extract_id;
        if (docsId) {
          const r2 = await authenticatedFetch(
            `${apiUrl}/woodsman/repo-docs/${docsId}`,
          );
          if (r2 && r2.ok) {
            const d = await r2.json();
            if (!cancelled) setDocsChunks(d?.chunks || []);
          }
        }
      } catch {
        // ignore
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [showBuildPlan, buildSpecId, accessToken, apiUrl, planData]);

  return (
    <div className="border rounded-md p-3 bg-white shadow-sm" data-testid="build-queue-row">
      {phaseHeader && (
        <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
          {phaseHeader}
        </div>
      )}

      {/* §10d — stall warning banner: yellow when ETA endpoint
          reported a stall_reason for this RUNNING row. */}
      {etaInfo?.stall_reason && (
        <div className="mb-2 text-xs px-2 py-1 rounded border bg-amber-50 text-amber-900 border-amber-200">
          ⚠ {etaInfo.stall_reason}
        </div>
      )}
      {/* Stuck-row banner. When the row hasn't reached done/failed by
          the hard ceiling, OR has missed too many polls in a row,
          surface a clear "lost contact with backend" status. Prevents
          the indefinite-tick failure mode where the elapsed counter
          climbed to 16+ minutes while the workflow_run had already
          been FAILED for 6 minutes (2026-05-31 spec=12 repro). */}
      {stuck && (
        <div className="mb-2 text-xs px-2 py-1 rounded border bg-amber-50 text-amber-900 border-amber-200">
          ⚠ The progress poller lost contact with the build. The
          workflow may have already finished — refresh the page to
          see the final status.
        </div>
      )}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          {stage === "done" ? (
            <CheckCircle2 className="w-4 h-4 text-green-600 shrink-0" />
          ) : stage === "failed" || stuck ? (
            <XCircle className={`w-4 h-4 shrink-0 ${stuck ? "text-amber-600" : "text-red-600"}`} />
          ) : (
            <Loader2 className="w-4 h-4 text-blue-500 animate-spin shrink-0" />
          )}
          <span className="font-medium truncate" title={entry.nodeName}>
            {entry.nodeName || `Task #${entry.taskId}`}
          </span>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <span className="text-xs text-gray-500 tabular-nums">{fmtElapsed(elapsed)}</span>
          {/* Resume button gate (issue #8): suppressed while the Phase 1
              artifacts gate is pending so the user can't click Resume
              into an empty wizard. The gate clears either when
              artifacts arrive OR after 3 consecutive misses (the
              caveat path). */}
          {/* Retry with corrections (issue #2). Surfaces when there's
              at least one structured per-variant failure entry to
              seed the LLM corrections prompt — covers BOTH Phase 1
              verification failures (hallucinated FROM tags caught by
              harbor_tag_verify before Phase 2 ever runs) AND Phase 2
              build failures. The parent dispatches the new
              Containerizer task and replaces this row. */}
          {onRetryWithCorrections &&
            (entry.phase === "build" || entry.phase === "build_only" || entry.phase === "generation") &&
            (stage === "failed" || Object.values(buildResults).some((r) => r.status === "failed")) &&
            Object.values(buildResults).some((r) => r.status === "failed") && (
              <button
                onClick={() => onRetryWithCorrections(entry.taskId)}
                className="text-xs font-medium text-amber-800 bg-amber-50 hover:bg-amber-100 border border-amber-200 rounded px-2 py-1"
                title="Dispatch a new Containerizer task with this failure's context appended so the LLM can correct its previous output."
              >
                Retry with corrections
              </button>
            )}
          {/* LDF-4 #5 — Resume button removed per Tim's request. */}
          <button
            onClick={() => onDismiss(entry.taskId)}
            className="text-gray-400 hover:text-gray-700"
            aria-label="Dismiss"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Stage chips — phase-scoped: 3 chips for "generation", 7 for "build",
          legacy 8-chip strip for entries with no phase set. */}
      <div className="flex items-center gap-1 mt-2 text-[10px] uppercase tracking-wide">
        {stages.map((s, i) => (
          <span
            key={s}
            className={
              stage === "failed" && i > 0
                ? "text-red-500 opacity-40"
                : i < idx
                  ? "text-green-600"
                  : i === idx
                    ? "text-blue-600 font-semibold"
                    : "text-gray-400"
            }
          >
            {STAGE_LABEL[s]}
            {i < stages.length - 1 && <span className="mx-1 text-gray-300">›</span>}
          </span>
        ))}
        {/* Phase 1 finalizing chip — shown alongside the regular chip
            strip while we wait for artifacts to flush server-side
            (issue #8). Replaces the misleading "done" green that used
            to appear before the Resume button was actually wired. */}
        {artifactsPending && (
          <span className="ml-2 text-blue-600 font-semibold flex items-center gap-1">
            <Loader2 className="w-3 h-3 animate-spin" />
            Finalizing…
          </span>
        )}
      </div>

      {/* Progress bar */}
      <div className="w-full bg-slate-100 rounded-full h-1.5 mt-2 overflow-hidden">
        <div
          className={
            stage === "failed"
              ? "bg-red-500 h-full transition-all"
              : stage === "done"
                ? "bg-green-500 h-full transition-all"
                : "bg-blue-500 h-full transition-all"
          }
          style={{ width: `${pct}%` }}
        />
      </div>

      {/* Variant sub-progress — "Variant 3 of 13: rocm" during multi-
          variant builds. Shows how far through the per-GPU-tag build
          loop ImageBuilder is. */}
      {variantProgress && stage !== "done" && stage !== "failed" && (
        <div className="text-[11px] text-gray-600 mt-1 flex items-center gap-1">
          <span className="font-medium">
            Variant {variantProgress.current} of {variantProgress.total}
          </span>
          {variantProgress.variantName && (
            <span className="text-gray-400">— {variantProgress.variantName}</span>
          )}
        </div>
      )}

      {/* P9e.4 — per-variant smoke-verification badges. Rendered
          whenever we have registered variants to show — that's
          once the ImageBuilder task has populated gpu_variants on
          the NodeDefinition, which happens right at the end of a
          successful build. States:

            pending (or undefined) — amber spinner "verifying"
            verified               — green check
            failed                 — red X, tooltip says smoke failed
            build_failed           — red X, tooltip shows the
                                     ImageBuilder failure reason */}
      {(Object.keys(gpuVariantsRegistered).length > 0 ||
        Object.keys(incompatibleReasons).length > 0) && (
        <div className="flex flex-wrap gap-1.5 mt-2">
          {Array.from(new Set([
            ...Object.keys(gpuVariantsRegistered),
            ...Object.keys(incompatibleReasons),
          ])).sort().map((variant) => {
            const vvs = variantVerificationStatus[variant];
            const incompat = incompatibleReasons[variant];
            // Precedence: incompat_reasons always wins (build never
            // succeeded → can't smoke). Then verified/failed, then
            // pending as default for registered-but-unsmoked.
            let color = "";
            let icon: React.ReactNode = null;
            let label = variant;
            let title = "";
            if (incompat || vvs === "build_failed") {
              color = "bg-red-50 text-red-700 border-red-200";
              icon = <XCircle className="w-3 h-3 shrink-0" />;
              title = `Build failed: ${incompat || "build_failed"}`;
            } else if (vvs === "failed") {
              color = "bg-red-50 text-red-700 border-red-200";
              icon = <XCircle className="w-3 h-3 shrink-0" />;
              title = "Smoke verification failed — the image built + pushed but failed its post-build import/forward-pass probe. Click for the full explanation.";
            } else if (vvs === "numerical_drift") {
              color = "bg-red-50 text-red-700 border-red-200";
              icon = <XCircle className="w-3 h-3 shrink-0" />;
              title = "Numerical drift — the variant's forward pass diverged from the CPU reference beyond tolerance. Click for drift details.";
            } else if (vvs === "verified") {
              color = "bg-green-50 text-green-700 border-green-200";
              icon = <CheckCircle2 className="w-3 h-3 shrink-0" />;
              title = "Built + smoke-verified on a matching-hardware worker.";
            } else {
              // pending / undefined — image built + pushed, smoke
              // task enqueued or running.
              color = "bg-amber-50 text-amber-700 border-amber-200";
              icon = <Loader2 className="w-3 h-3 shrink-0 animate-spin" />;
              title = "Verifying — smoke probe running on a matching-hardware worker.";
            }
            // P9o — red badges become clickable when there's a structured
            // explanation for this variant's failure. Opens the failure
            // modal with reason / manual_fix / log_excerpt.
            const isFailure = (
              !!incompat || vvs === "failed" || vvs === "numerical_drift"
              || vvs === "build_failed"
            );
            const hasExplanation = isFailure && failureExplanations[variant];
            const clickProps = hasExplanation ? {
              onClick: () => setFailureModalVariant(variant),
              className: `inline-flex items-center gap-1 text-[10px] font-medium border rounded px-1.5 py-0.5 cursor-pointer hover:bg-red-100 ${color}`,
              role: "button" as const,
              tabIndex: 0,
            } : {
              className: `inline-flex items-center gap-1 text-[10px] font-medium border rounded px-1.5 py-0.5 ${color}`,
            };
            return (
              <span
                key={variant}
                title={title}
                {...clickProps}
              >
                {icon}
                <span>{label}</span>
                {hasExplanation && <span className="ml-0.5 opacity-60">ⓘ</span>}
              </span>
            );
          })}
        </div>
      )}

      {/* Status hint — last log line summary while live. */}
      {lastLog && stage !== "done" && stage !== "failed" && (
        <div className="text-xs text-gray-500 mt-1 truncate" title={lastLog}>
          {lastLog}
        </div>
      )}

      {/* Logs toggle + panel. Lives on every row so the user can crack
          open the full container stdout/stderr at any time (live via
          Redis; final via result_payload). */}
      <div className="mt-2">
        <div className="flex items-center gap-4">
          <button
            className="flex items-center gap-1 text-xs text-gray-600 hover:text-gray-900"
            onClick={() => setLogsOpen((o) => !o)}
          >
            {logsOpen
              ? <ChevronDown className="w-3.5 h-3.5" />
              : <ChevronRight className="w-3.5 h-3.5" />}
            <Terminal className="w-3.5 h-3.5" />
            Logs
            {logsOpen && logSource === "redis" && (
              <span className="ml-1 inline-flex items-center gap-1 text-[10px] font-medium text-green-600">
                <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
                LIVE
              </span>
            )}
            {logsOpen && logLines.length > 0 && (
              <span className="ml-1 text-[10px] text-gray-400">
                {logLines.length} lines
              </span>
            )}
          </button>
          {/* P9n.2 — port diff viewer. Button only offered when the build
              reached a terminal state; pre-completion the port data isn't
              stable yet. An empty modal is a valid outcome for non-deep-
              CUDA repos; the modal itself renders a helpful empty state. */}
          {(stage === "done" || stage === "failed") && (
            <button
              className="flex items-center gap-1 text-xs text-gray-600 hover:text-indigo-700"
              onClick={() => setPortDiffOpen(true)}
              title="Inspect what the kernel porter changed for each GPU variant"
            >
              <FileCode className="w-3.5 h-3.5" />
              Port diff
            </button>
          )}
          {logsOpen && logLines.length > 0 && (
            <CopyLogButton
              getText={() => logLines.join("\n")}
              className="ml-auto"
              title="Copy the full container log to the clipboard"
            />
          )}
        </div>
        {logsOpen && (
          <div className="mt-2 rounded border border-slate-200 bg-slate-900 text-slate-100 text-[11px] font-mono max-h-64 overflow-auto p-2">
            {logsLoading && logLines.length === 0 && (
              <div className="text-slate-400 italic">Loading logs…</div>
            )}
            {!logsLoading && logLines.length === 0 && (
              <div className="text-slate-400 italic space-y-1">
                {(stage === "done" || stage === "failed") ? (
                  "Container produced no stdout/stderr output."
                ) : (
                  /* LDF-3 §6 — without the second sentence users
                     thought a slow image-pull / GPU warm-up was a
                     hung run. */
                  <>
                    <div>
                      Waiting for the first log line — the worker streams output to Redis every 500ms once the container starts writing.
                    </div>
                    <div>
                      Container startup (image pull, GPU warm-up) can take several minutes before the first log line appears. This is normal.
                    </div>
                  </>
                )}
              </div>
            )}
            {logLines.map((line, i) => (
              <div key={i} className="whitespace-pre-wrap break-all leading-tight">
                {line || "\u00A0"}
              </div>
            ))}
            <div ref={logEndRef} />
          </div>
        )}
      </div>
      <VariantPortDiff
        open={portDiffOpen}
        onClose={() => setPortDiffOpen(false)}
        taskId={entry.taskId}
        accessToken={accessToken}
      />
      <VariantFailureModal
        open={failureModalVariant !== null}
        onClose={() => setFailureModalVariant(null)}
        variant={failureModalVariant || ""}
        explanation={failureModalVariant ? failureExplanations[failureModalVariant] : null}
        nodeName={entry.nodeName}
      />

      {/* Per-variant structured failure cards (issue #2). Rendered
          when at least one variant emitted a BUILD_RESULT_JSON
          envelope with status: "failed". Each card surfaces the
          curated error_class, the offending Dockerfile line + shell
          command, and the one-line user-facing explanation. */}
      {Object.values(buildResults).some((r) => r.status === "failed") && (
        <div className="mt-2 space-y-1.5">
          {Object.values(buildResults)
            .filter((r) => r.status === "failed")
            .map((r) => (
              <div
                key={r.variant}
                className="rounded border border-red-200 bg-red-50 px-2 py-1.5 text-[11px] text-red-900"
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-semibold uppercase tracking-wide text-red-700">
                    {r.variant === "hero" ? "canonical" : r.variant} failed
                  </span>
                  {r.dockerfile && (
                    <span className="text-red-600/80">
                      {r.dockerfile}
                      {r.line ? `:${r.line}` : ""}
                    </span>
                  )}
                </div>
                <div className="leading-snug mb-1">
                  {ERROR_CLASS_LABEL[r.error_class] ?? ERROR_CLASS_LABEL.unknown}
                </div>
                {r.command && (
                  <pre className="text-[10px] font-mono bg-red-100 border border-red-200 rounded px-1.5 py-1 overflow-auto whitespace-pre-wrap break-words">
                    {r.command}
                  </pre>
                )}
              </div>
            ))}
        </div>
      )}

      {stage === "failed" && failingLines.length > 0 && (
        <div className="mt-2">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[11px] font-semibold text-red-700">Failure log</span>
            <CopyLogButton
              getText={() => failingLines.join("\n")}
              title="Copy the failure log to the clipboard"
            />
          </div>
          <pre className="text-[11px] text-red-900 bg-red-50 border border-red-200 rounded p-2 overflow-auto max-h-40 font-mono">
            {failingLines.join("\n")}
          </pre>
        </div>
      )}

      {/* §10d — View build plan disclosure + ETA chip. Only when
          we know the BuildSpec id (every §3+ build does). */}
      {buildSpecId && (
        <div className="mt-2 text-xs flex items-center gap-2 flex-wrap">
          <button
            type="button"
            onClick={() => setShowBuildPlan((v) => !v)}
            className="text-sky-700 hover:underline"
          >
            {showBuildPlan ? "Hide" : "View"} build plan
          </button>
          {etaInfo && (
            <span className="text-muted-foreground">
              ETA ~{etaInfo.eta_minutes} min
              {etaInfo.variant_count > 1 && (
                <> · {etaInfo.variant_count} variants</>
              )}
            </span>
          )}
          {docsChunks && docsChunks.length > 0 && (
            <span
              className="text-[10px] px-1.5 py-0.5 rounded bg-violet-50 text-violet-700 border border-violet-200"
              title="BuildPlan extracted from repo docs"
            >
              📖 docs informed
            </span>
          )}
        </div>
      )}

      {/* §10d — expanded panels: BuildPlanPanel + RepoDocsPanel. */}
      {showBuildPlan && planData && (
        <div className="mt-2 space-y-2">
          <BuildPlanPanel plan={planData as Parameters<typeof BuildPlanPanel>[0]["plan"]} />
          {docsChunks && (
            <RepoDocsPanel chunks={docsChunks as Parameters<typeof RepoDocsPanel>[0]["chunks"]} />
          )}
        </div>
      )}

      {/* §7d / §10d — repair-exhausted recovery actions. Surfaces
          only when the row terminally failed with an exhausted
          repair budget. The three buttons map 1-to-1 to the §7d
          backend endpoints. */}
      {stage === "failed"
        && (entry as { exhaustedRepair?: boolean }).exhaustedRepair === true && (
        <div className="mt-2 text-xs border rounded-md p-2 bg-amber-50 border-amber-200">
          <div className="font-semibold mb-1">
            Auto-repair could not converge
          </div>
          <div className="text-muted-foreground mb-2">
            Pick a recovery action:
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="px-2 py-1 rounded border bg-white hover:bg-muted"
              onClick={async () => {
                if (!buildSpecId) return;
                await authenticatedFetch(
                  `${apiUrl}/woodsman/wizard/retry-with-elevated-budget`,
                  {
                    method: "POST",
                    body: JSON.stringify({ build_spec_id: buildSpecId }),
                  },
                );
                setShowRecovery(false);
              }}
            >
              Retry with elevated budget
            </button>
            <button
              type="button"
              className="px-2 py-1 rounded border bg-white hover:bg-muted"
              onClick={async () => {
                if (!buildSpecId) return;
                await authenticatedFetch(
                  `${apiUrl}/woodsman/wizard/finalize-partial-publish`,
                  {
                    method: "POST",
                    body: JSON.stringify({ build_spec_id: buildSpecId }),
                  },
                );
                setShowRecovery(false);
              }}
            >
              Ship hero only, skip failing variants
            </button>
            <button
              type="button"
              className="px-2 py-1 rounded border bg-white hover:bg-muted"
              onClick={async () => {
                if (!buildSpecId) return;
                await authenticatedFetch(
                  `${apiUrl}/woodsman/wizard/support-tickets`,
                  {
                    method: "POST",
                    body: JSON.stringify({
                      build_spec_id: buildSpecId,
                      user_message: "Auto-repair exhausted; please review.",
                    }),
                  },
                );
                setShowRecovery(false);
              }}
            >
              Send to MOD support
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// Memo equality: sibling rows skip re-render unless the entry they
// represent has actually changed identity (same taskId && same
// outcome). Stage/elapsed/log changes happen inside the row's own
// useState so they don't flow through props — memo catches this
// automatically without a custom comparator, but we add one as an
// explicit safety net in case we extend the entry shape later.
const BuildQueueRow = React.memo(BuildQueueRowInner, (prev, next) =>
  prev.entry.taskId === next.entry.taskId &&
  prev.entry.outcome === next.entry.outcome &&
  prev.entry.phase === next.entry.phase &&
  prev.accessToken === next.accessToken &&
  prev.onResumeGeneration === next.onResumeGeneration,
);

export default BuildQueueRow;
